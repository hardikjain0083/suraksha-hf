"""Shared MAP evidence completion checks."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from services.validator_service import validate_evidence

ACCEPTED_VALIDATION_STATUSES = {"pass", "override_pass", "approved", "validated"}
REVIEW_STATUSES = {"pending_validation", "manual_review", "pending", None, ""}


def is_validation_accepted(status: str | None) -> bool:
    return status in ACCEPTED_VALIDATION_STATUSES


def required_evidence_items(map_doc: dict) -> list[tuple[int, dict]]:
    return [
        (idx, item)
        for idx, item in enumerate(map_doc.get("evidence_items", []))
        if item.get("required", True)
    ]


async def validate_required_evidence(database: Any, map_doc: dict) -> dict:
    """Run pending validations and report whether a MAP can be completed."""
    evidence_items = list(map_doc.get("evidence_items", []))
    blockers: list[dict] = []
    required_items = required_evidence_items(map_doc)

    if not required_items:
        blockers.append({
            "evidence_index": None,
            "label": "Required evidence",
            "reason": "no_required_evidence_configured",
        })

    def clean_val_status(s: str | None) -> str:
        if s == "pass":
            return "validated"
        if s == "fail":
            return "rejected"
        if s in ("validated", "rejected", "pending"):
            return s
        return "pending"

    for idx, item in required_items:
        label = item.get("label") or item.get("evidence_type") or f"Evidence {idx + 1}"
        if not item.get("uploaded"):
            blockers.append({"evidence_index": idx, "label": label, "reason": "not_uploaded"})
            continue

        evidence_id = item.get("evidence_id")
        if not evidence_id:
            blockers.append({"evidence_index": idx, "label": label, "reason": "missing_evidence_record"})
            continue

        evidence = await database.evidence.find_one({"evidence_id": evidence_id})
        if not evidence:
            blockers.append({"evidence_index": idx, "label": label, "reason": "evidence_record_not_found"})
            continue

        status = clean_val_status(evidence.get("validation_status"))
        if status in REVIEW_STATUSES or status == "pending":
            result = await validate_evidence(database, evidence_id)
            status = clean_val_status(result["validation_status"])
            evidence = await database.evidence.find_one({"evidence_id": evidence_id}) or evidence

        item["validation_status"] = status
        item["confidence"] = evidence.get("confidence", item.get("confidence"))
        item["validated_at"] = evidence.get("validated_at", item.get("validated_at"))
        item["validation_details"] = evidence.get("validation_details", item.get("validation_details"))
        evidence_items[idx] = item

        if not is_validation_accepted(status):
            blockers.append({
                "evidence_index": idx,
                "evidence_id": evidence_id,
                "label": label,
                "reason": "validation_not_accepted",
                "validation_status": status,
                "confidence": evidence.get("confidence"),
            })

    return {
        "can_complete": len(blockers) == 0,
        "blockers": blockers,
        "evidence_items": evidence_items,
        "checked_at": datetime.utcnow(),
    }
