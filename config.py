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

    # Blueprint research (applied to every blueprint unless overridden later)
    blueprint_me: int = 10
    blueprint_te: int = 20

    # Manufacturing structure
    structure_material_bonus: float = 1.0   # % (e.g. Raitaru = 1)
    structure_time_bonus: float = 15.0      # %
    structure_rig_material_bonus: float = 2.0  # % (e.g. T1 ME rig = 2.0 with hisec multiplier applied by user)
    structure_tax: float = 1.0              # % facility tax

    # Manufacturing system (for the system cost index). Default: Sobaseki
    system_id: int = 30001363
    system_name: str = "Sobaseki"

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
