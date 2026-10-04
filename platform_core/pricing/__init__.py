"""Unit-price snapshot loader. The engine only ever reads prices through a PriceTable."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SNAPSHOT = Path(__file__).with_name("snapshot.json")


@dataclass(frozen=True)
class PriceTable:
    snapshot_id: str
    prices: dict
    region: str = "ap-northeast-2"
    updated_at: str = ""
    source: str = "manual"
    approximate: bool = True

    def metadata(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "region": self.region, "updated_at": self.updated_at,
                "source": self.source, "approximate": self.approximate}

    def __getitem__(self, key: str) -> float:
        return self.prices[key]

    @staticmethod
    def load(path: Path | str = DEFAULT_SNAPSHOT) -> PriceTable:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        return PriceTable(
            snapshot_id=doc["snapshot_id"],
            prices={k: float(v["value"]) for k, v in doc["prices"].items()},
            region=doc["region"],
            updated_at=doc["updated_at"],
            source=doc["source"],
            approximate=bool(doc.get("approximate", True)),
        )
