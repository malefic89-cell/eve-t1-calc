"""Character / industry settings, persisted to data/settings.json."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"


@dataclass
class Settings:
    # Skills (levels 0-5)
    accounting: int = 5
    broker_relations: int = 5
    industry: int = 5
    advanced_industry: int = 5

    # Standings toward the NPC station owner (Jita 4-4: Caldari Navy / Caldari State)
    faction_standing: float = 0.0
    corp_standing: float = 0.0

    # Blueprint research defaults (used unless a per-blueprint override exists)
    blueprint_me: int = 10
    blueprint_te: int = 20
    # Per-blueprint overrides: {blueprint_type_id as str: {"me": int, "te": int}}
    # (str keys because JSON round-trips object keys as strings)
    blueprint_overrides: dict = field(default_factory=dict)

    # Manufacturing structure
    structure_material_bonus: float = 1.0   # % (e.g. Raitaru = 1)
    structure_time_bonus: float = 15.0      # %
    structure_rig_material_bonus: float = 2.0  # % (e.g. T1 ME rig = 2.0 with hisec multiplier applied by user)
    structure_tax: float = 1.0              # % facility tax
    structure_cost_bonus: float = 3.0       # % job cost bonus (Raitaru 3, Azbel 4, Sotiyo 5)

    # Manufacturing system (for the system cost index). Default: Sobaseki
    system_id: int = 30001363
    system_name: str = "Sobaseki"

    # Runs per job: affects material rounding (per job, not per run),
    # order book depth walked, EIV and total time
    runs: int = 1

    def validate(self) -> None:
        for name in ("accounting", "broker_relations", "industry", "advanced_industry"):
            v = getattr(self, name)
            if not 0 <= int(v) <= 5:
                raise ValueError(f"{name} must be 0-5")
            setattr(self, name, int(v))
        if not 0 <= int(self.blueprint_me) <= 10:
            raise ValueError("blueprint_me must be 0-10")
        if not 0 <= int(self.blueprint_te) <= 20:
            raise ValueError("blueprint_te must be 0-20")
        for name in ("faction_standing", "corp_standing"):
            v = float(getattr(self, name))
            if not -10.0 <= v <= 10.0:
                raise ValueError(f"{name} must be -10..10")
        self.runs = int(self.runs)
        if not 1 <= self.runs <= 1_000_000:
            raise ValueError("runs must be >= 1")
        clean = {}
        for k, ov in (self.blueprint_overrides or {}).items():
            me, te = int(ov["me"]), int(ov["te"])
            if not 0 <= me <= 10:
                raise ValueError(f"blueprint {k}: ME must be 0-10")
            if not 0 <= te <= 20:
                raise ValueError(f"blueprint {k}: TE must be 0-20")
            clean[str(int(k))] = {"me": me, "te": te}
        self.blueprint_overrides = clean

    def to_dict(self) -> dict:
        return asdict(self)


def load_settings() -> Settings:
    if SETTINGS_FILE.exists():
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        known = {f for f in Settings.__dataclass_fields__}
        s = Settings(**{k: v for k, v in raw.items() if k in known})
    else:
        s = Settings()
    s.validate()
    return s


def save_settings(s: Settings) -> None:
    s.validate()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(
        json.dumps(s.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
