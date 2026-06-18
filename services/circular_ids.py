"""Sequential circular ID generation: {ISSUER}/{YYYY}-{SEQ:03d}."""
from __future__ import annotations

from typing import Any

from pymongo import ReturnDocument


async def generate_circular_id(database: Any, issuer: str, year: int) -> str:
    counter_id = f"circular_{issuer}_{year}"
    result = await database.counters.find_one_and_update(
        {"_id": counter_id},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    seq = result.get("seq", 1) if result else 1
    return f"{issuer}/{year}-{seq:03d}"
