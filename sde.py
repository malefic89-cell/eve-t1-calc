"""SDE (Static Data Export) access.

Downloads Fuzzwork's SQLite conversion on first run and exposes the queries
the calculator needs. Only manufacturing (activityID=1) is used for now;
invention/reactions can be added later by extending ACTIVITY constants.
"""
from __future__ import annotations

import sqlite3
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


class SDE:
    def __init__(self, path: Path = SDE_PATH):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def manufacturable_t1_products(self) -> list[Product]:
        """All published T1 products of manufacturing blueprints."""
        rows = self.conn.execute(
            """
            SELECT p.productTypeID AS product_id,
                   t.typeName      AS product_name,
                   p.typeID        AS blueprintTypeID,
                   p.quantity,
                   a.time          AS base_time,
                   g.groupName,
                   c.categoryName
            FROM industryActivityProducts p
            JOIN invTypes t  ON t.typeID = p.productTypeID
            JOIN invTypes bp ON bp.typeID = p.typeID
            JOIN invGroups g ON g.groupID = t.groupID
            JOIN invCategories c ON c.categoryID = g.categoryID
            JOIN industryActivity a
                 ON a.typeID = p.typeID AND a.activityID = ?
            LEFT JOIN invMetaTypes m ON m.typeID = p.productTypeID
            WHERE p.activityID = ?
              AND t.published = 1
              AND bp.published = 1
              AND (m.metaGroupID IS NULL OR m.metaGroupID IN (%s))
            """
            % ",".join("?" * len(T1_META_GROUPS)),
            (ACTIVITY_MANUFACTURING, ACTIVITY_MANUFACTURING, *T1_META_GROUPS),
        ).fetchall()
        return [
            Product(
                type_id=r["product_id"],
                name=r["product_name"],
                blueprint_type_id=r["blueprintTypeID"],
                quantity_per_run=r["quantity"],
                base_time=r["base_time"],
                group_name=r["groupName"],
                category_name=r["categoryName"],
            )
            for r in rows
        ]

    def materials_for_blueprint(self, blueprint_type_id: int) -> list[Material]:
        rows = self.conn.execute(
            """
            SELECT m.materialTypeID, t.typeName, m.quantity
            FROM industryActivityMaterials m
            JOIN invTypes t ON t.typeID = m.materialTypeID
            WHERE m.typeID = ? AND m.activityID = ?
            ORDER BY m.quantity DESC
            """,
            (blueprint_type_id, ACTIVITY_MANUFACTURING),
        ).fetchall()
        return [
            Material(type_id=r["materialTypeID"], name=r["typeName"], base_qty=r["quantity"])
            for r in rows
        ]

    def search_systems(self, query: str, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            """
            SELECT solarSystemID, solarSystemName, security
            FROM mapSolarSystems
            WHERE solarSystemName LIKE ?
            ORDER BY solarSystemName
            LIMIT ?
            """,
            (f"%{query}%", limit),
        ).fetchall()
        return [
            {
                "system_id": r["solarSystemID"],
                "name": r["solarSystemName"],
                "security": round(r["security"], 2),
            }
            for r in rows
        ]

    def type_name(self, type_id: int) -> str | None:
        r = self.conn.execute(
            "SELECT typeName FROM invTypes WHERE typeID = ?", (type_id,)
        ).fetchone()
        return r["typeName"] if r else None

    def categories(self) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.categoryName
            FROM industryActivityProducts p
            JOIN invTypes t ON t.typeID = p.typeID
            JOIN invGroups g ON g.groupID = t.groupID
            JOIN invCategories c ON c.categoryID = g.categoryID
            WHERE p.activityID = ? AND t.published = 1
            ORDER BY c.categoryName
            """,
            (ACTIVITY_MANUFACTURING,),
        ).fetchall()
        return [r["categoryName"] for r in rows]
