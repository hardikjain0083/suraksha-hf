"""Persistent tamper-evident audit log chain in MongoDB."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64


def _serialize_for_hash(doc: dict) -> str:
    payload = {k: v for k, v in doc.items() if k not in ("tamper_evident_hash", "previous_log_hash", "_id")}
    return json.dumps(payload, sort_keys=True, default=str)


def compute_tamper_hash(doc: dict, previous_log_hash: str) -> str:
    content = _serialize_for_hash(doc) + previous_log_hash
    return hashlib.sha256(content.encode()).hexdigest()


async def get_latest_hash(database: Any) -> str:
    latest = await database.audit_logs.find_one(
        {},
        sort=[("chain_index", -1), ("timestamp", -1), ("log_id", -1)],
        projection={"tamper_evident_hash": 1},
    )
    if latest and latest.get("tamper_evident_hash"):
        return latest["tamper_evident_hash"]
    return GENESIS_HASH


async def append_audit_log(
    database: Any,
    *,
    action_type: str,
    target_type: str,
    target_id: str,
    user_id: str = "system",
    user_name: str = "System",
    department_id: Optional[str] = None,
    session_id: Optional[str] = None,
    details: Optional[dict] = None,
    provenance: Optional[dict] = None,
    state_change: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    prev_hash = await get_latest_hash(database)
    chain_index = await database.audit_logs.count_documents({}) + 1
    now = datetime.utcnow()
    log_id = f"AUD-{uuid4()}"

    audit_doc = {
        "log_id": log_id,
        "chain_index": chain_index,
        "timestamp": now,
        "user_id": user_id,
        "user_name": user_name,
        "department_id": department_id,
        "session_id": session_id,
        "action_type": action_type,
        "target_type": target_type,
        "target_id": target_id,
        "details": details or {},
        "provenance": provenance or {},
        "state_change": state_change or {},
        "ip_address": ip_address,
        "user_agent": user_agent,
        "previous_log_hash": prev_hash,
    }
    audit_doc["tamper_evident_hash"] = compute_tamper_hash(audit_doc, prev_hash)

    await database.audit_logs.insert_one(audit_doc)
    return {
        "log_id": log_id,
        "hash": audit_doc["tamper_evident_hash"],
        "chain_status": "intact",
    }


async def verify_audit_chain(database: Any) -> dict:
    cursor = database.audit_logs.find({}).sort([("chain_index", 1), ("timestamp", 1), ("log_id", 1)])
    prev_h = GENESIS_HASH
    total = 0
    broken_at = None
    is_valid = True

    async for log in cursor:
        total += 1
        test_doc = dict(log)
        expected = compute_tamper_hash(test_doc, prev_h)
        stored_prev = log.get("previous_log_hash", GENESIS_HASH)
        if stored_prev != prev_h or expected != log.get("tamper_evident_hash"):
            is_valid = False
            broken_at = log.get("log_id")
            break
        prev_h = log.get("tamper_evident_hash", prev_h)

    return {
        "verified": is_valid,
        "integrity": "valid" if is_valid else "broken",
        "broken_at": broken_at,
        "total_logs": total,
    }
