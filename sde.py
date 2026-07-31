"""SDE (Static Data Export) access.

Downloads Fuzzwork's SQLite conversion on first run and exposes the queries
the calculator needs. Only manufacturing (activityID=1) is used for now;
invention/reactions can be added later by extending ACTIVITY constants.
"""
from __future__ import annotations

import sqlite3
import threading
import zlib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SDE_PATH = DATA_DIR / "sde.sqlite"
SDE_URL = "https://www.fuzzwork.co.uk/dump/latest-sqlite.db.gz"

ACTIVITY_MANUFACTURING = 1

# metaGroupID 1 = Tech I; items missing from invMetaTypes are also T1
T1_META_GROUPS = (1,)

# Rigs are lifted out of the "Module" category into one category per size: that
# category held 812 T1 products across 150 groups, 326 of them rigs, which made
# the group filter unusable. Size comes from the rigSize attribute, ordered
# smallest first — the category filter lists them in this order, not alphabetically.
RIG_SIZE_ATTRIBUTE = 1547
RIG_SIZE_NAMES = {1: "Small", 2: "Medium", 3: "Large", 4: "Capital"}
RIG_CATEGORY = "Module"          # the category rigs are pulled out of
RIG_GROUP_PREFIX = "Rig "        # "Rig Armor", "Rig Shield", ...


def rig_category(size: int | None) -> str | None:
    """Category name for a rig of this rigSize, or None if the size is unknown."""
    name = RIG_SIZE_NAMES.get(size)
    return f"Rigs ({name})" if name else None


def sde_exists() -> bool:
    return SDE_PATH.exists() and SDE_PATH.stat().st_size > 0


def download_sde(progress_cb=None) -> None:
    """Download and decompress the SDE. progress_cb(stage, done_bytes, total_bytes)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_gz = SDE_PATH.with_suffix(".sqlite.gz.part")
    tmp_db = SDE_PATH.with_suffix(".sqlite.part")

    req = urllib.request.Request(SDE_URL, headers={"User-Agent": "eve-t1-calc/1.0"})
    with urllib.request.urlopen(req) as resp, open(tmp_gz, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if progress_cb:
                progress_cb("download", done, total)

    decomp = zlib.decompressobj(wbits=31)  # gzip container
    written = 0
    with open(tmp_gz, "rb") as src, open(tmp_db, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            data = decomp.decompress(chunk)
            dst.write(data)
            written += len(data)
            if progress_cb:
                progress_cb("decompress", written, 0)
        dst.write(decomp.flush())

    tmp_db.replace(SDE_PATH)
    tmp_gz.unlink(missing_ok=True)


@dataclass
class Material:
    type_id: int
    name: str
    base_qty: int


@dataclass
class Product:
    type_id: int
    name: str
    blueprint_type_id: int
    quantity_per_run: int
    base_time: int  # seconds, unmodified
    group_name: str
    category_name: str
    # Blueprint is buyable as a BPO: published and has a market group.
    # False = BPC-only (event drops like Praxis/Gnosis, boosters, EDENCOM).
    bpo_on_market: bool = True


class SDE:
    """Read-only SDE access.

    One connection is shared by the bootstrap thread and FastAPI's request
    threadpool (`/api/systems`), so every query is serialized by `_lock` —
    check_same_thread=False only silences the thread check, it does not stop
    two threads from interleaving on the same cursor.
    """

    def __init__(self, path: Path = SDE_PATH):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def manufacturable_t1_products(self) -> list[Product]:
        """All published T1 products of manufacturing blueprints."""
        rows = self._query(
            """
            SELECT p.productTypeID AS product_id,
                   t.typeName      AS product_name,
                   p.typeID        AS blueprintTypeID,
                   p.quantity,
                   a.time          AS base_time,
                   g.groupName,
                   c.categoryName,
                   (bp.published = 1 AND bp.marketGroupID IS NOT NULL) AS bpo_market,
                   COALESCE(ra.valueInt, ra.valueFloat) AS rig_size
            FROM industryActivityProducts p
            JOIN invTypes t  ON t.typeID = p.productTypeID
            JOIN invTypes bp ON bp.typeID = p.typeID
            JOIN invGroups g ON g.groupID = t.groupID
            JOIN invCategories c ON c.categoryID = g.categoryID
            JOIN industryActivity a
                 ON a.typeID = p.typeID AND a.activityID = ?
            LEFT JOIN invMetaTypes m ON m.typeID = p.productTypeID
            -- (typeID, attributeID) is the primary key, so this cannot multiply rows
            LEFT JOIN dgmTypeAttributes ra
                 ON ra.typeID = p.productTypeID AND ra.attributeID = ?
            WHERE p.activityID = ?
              AND t.published = 1
              AND (m.metaGroupID IS NULL OR m.metaGroupID IN (%s))
            """
            % ",".join("?" * len(T1_META_GROUPS)),
            # binding order follows the ?s in the text: activity, rig attribute,
            # activity again, then the meta groups
            (
                ACTIVITY_MANUFACTURING,
                RIG_SIZE_ATTRIBUTE,
                ACTIVITY_MANUFACTURING,
                *T1_META_GROUPS,
            ),
        )
        return [
            Product(
                type_id=r["product_id"],
                name=r["product_name"],
                blueprint_type_id=r["blueprintTypeID"],
                quantity_per_run=r["quantity"],
                base_time=r["base_time"],
                group_name=r["groupName"],
                category_name=self._category_of(r),
                bpo_on_market=bool(r["bpo_market"]),
            )
            for r in rows
        ]

    @staticmethod
    def _category_of(r: sqlite3.Row) -> str:
        """Real category, except rigs get one per size.

        All three conditions are required. rigSize alone is not enough: ships
        carry it too (399 of them) to say which rig size they accept, so keying
        on the attribute by itself files every frigate under "Rigs (Small)".
        A rig missing the attribute simply stays in Module rather than vanishing.
        """
        category, group = r["categoryName"], r["groupName"]
        if category == RIG_CATEGORY and group.startswith(RIG_GROUP_PREFIX):
            size = r["rig_size"]
            if size is not None:
                return rig_category(int(size)) or category
        return category

    def materials_for_blueprint(self, blueprint_type_id: int) -> list[Material]:
        rows = self._query(
            """
            SELECT m.materialTypeID, t.typeName, m.quantity
            FROM industryActivityMaterials m
            JOIN invTypes t ON t.typeID = m.materialTypeID
            WHERE m.typeID = ? AND m.activityID = ?
            ORDER BY m.quantity DESC
            """,
            (blueprint_type_id, ACTIVITY_MANUFACTURING),
        )
        return [
            Material(type_id=r["materialTypeID"], name=r["typeName"], base_qty=r["quantity"])
            for r in rows
        ]

    def search_systems(self, query: str, limit: int = 20) -> list[dict]:
        rows = self._query(
            """
            SELECT solarSystemID, solarSystemName, security
            FROM mapSolarSystems
            WHERE solarSystemName LIKE ?
            ORDER BY solarSystemName
            LIMIT ?
            """,
            (f"%{query}%", limit),
        )
        return [
            {
                "system_id": r["solarSystemID"],
                "name": r["solarSystemName"],
                "security": round(r["security"], 2),
            }
            for r in rows
        ]

    def type_name(self, type_id: int) -> str | None:
        rows = self._query("SELECT typeName FROM invTypes WHERE typeID = ?", (type_id,))
        return rows[0]["typeName"] if rows else None

    def type_id_by_name(self, name: str) -> int | None:
        """Used to resolve skill names to IDs, so no skill ID is hardcoded."""
        rows = self._query(
            "SELECT typeID FROM invTypes WHERE typeName = ? AND published = 1", (name,)
        )
        return rows[0]["typeID"] if rows else None

    def station_owner(self, station_id: int) -> dict | None:
        """Who owns a station, for the standings that set the broker fee.

        Derived rather than hardcoded so changing the trade hub keeps working:
        Jita 4-4 resolves to Caldari Navy (1000035) in the Caldari State (500001).
        """
        rows = self._query(
            """
            SELECT s.corporationID, n.itemName AS corporationName,
                   c.factionID, f.factionName
            FROM staStations s
            JOIN crpNPCCorporations c ON c.corporationID = s.corporationID
            LEFT JOIN invNames n ON n.itemID = s.corporationID
            LEFT JOIN chrFactions f ON f.factionID = c.factionID
            WHERE s.stationID = ?
            """,
            (station_id,),
        )
        if not rows:
            return None
        r = rows[0]
        return {
            "corporation_id": r["corporationID"],
            "corporation_name": r["corporationName"],
            "faction_id": r["factionID"],
            "faction_name": r["factionName"],
        }
