"""MAP routing: department assignment, workload-based assignee, escalation chain."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from services.map_generator import (
    resolve_department_graph_lookup,
    _detect_clause_category,
)
from services.audit_logger import append_audit_log

logger = logging.getLogger(__name__)

ACTIVE_MAP_STATUSES = ("open", "in_progress", "pending_validation", "approved")


async def find_employee_with_lowest_active_maps(
    database: Any, department_id: str
) -> Optional[str]:
    """Pick employee in department with lowest active_maps_count."""
    cursor = database.users.find(
        {
            "department_id": department_id,
            "status": "active",
            "role": {"$in": ["employee", "department_head"]},
        }
    ).sort("active_maps_count", 1)

    candidate = None
    async for user in cursor:
        candidate = user.get("emp_id")
        break
    return candidate


async def adjust_active_maps_count(
    database: Any, emp_id: Optional[str], delta: int
) -> None:
    if not emp_id:
        return
    await database.users.update_one(
        {"emp_id": emp_id},
        {"$inc": {"active_maps_count": delta}},
    )


async def route_map(database: Any, map_id: str) -> dict:
    map_doc = await database.maps.find_one({"map_id": map_id})
    if not map_doc:
        raise ValueError(f"MAP '{map_id}' not found")

    clause_text = map_doc.get("clause_text") or map_doc.get("description", "")
    category = _detect_clause_category(clause_text)
    gap_stub = {
        "circular_id": map_doc.get("circular_id"),
        "clause_number": map_doc.get("clause_number"),
    }
    dept_id, dept_name = await resolve_department_graph_lookup(
        database,
        gap_stub,
        map_doc.get("policy_id"),
        category,
    )

    dept = await database.departments.find_one({"department_id": dept_id}) or {}
    if map_doc.get("owner_department_id"):
        dept_id = map_doc["owner_department_id"]
        dept = await database.departments.find_one({"department_id": dept_id}) or dept

    assignee: Optional[str] = None
    if dept.get("auto_assign_enabled", True):
        assignee = await find_employee_with_lowest_active_maps(database, dept_id)
    if not assignee:
        assignee = dept.get("head_employee_id")

    escalation_chain = dept.get("escalation_chain", [])
    if not escalation_chain and dept.get("head_employee_id"):
        escalation_chain = [dept["head_employee_id"]]

    deadline = map_doc.get("deadline") or (datetime.utcnow() + timedelta(days=45))
    if isinstance(deadline, str):
        deadline = datetime.fromisoformat(deadline.replace("Z", ""))
    auto_escalate_at = deadline + timedelta(hours=48)

    prev_assignee = map_doc.get("assigned_to")
    if prev_assignee and prev_assignee != assignee:
        await adjust_active_maps_count(database, prev_assignee, -1)

    await database.maps.update_one(
        {"map_id": map_id},
        {
            "$set": {
                "owner_department_id": dept_id,
                "department_name": dept.get("name", dept_name),
                "assigned_to": assignee,
                "escalation_chain": escalation_chain,
                "auto_escalate_at": auto_escalate_at,
                "status": "open",
                "notification_sent": True,
                "routed_at": datetime.utcnow(),
            }
        },
    )

    if assignee and map_doc.get("status") not in ACTIVE_MAP_STATUSES:
        await adjust_active_maps_count(database, assignee, 1)

    await append_audit_log(
        database,
        action_type="map_routed",
        target_type="map",
        target_id=map_id,
        user_id="system",
        user_name="Routing Service",
        department_id=dept_id,
        details={
            "assignee": assignee,
            "department": dept_id,
            "auto_escalate_at": auto_escalate_at.isoformat(),
        },
        provenance={"map_id": map_id, "circular_id": map_doc.get("circular_id")},
        state_change={"field": "status", "old_value": map_doc.get("status"), "new_value": "open"},
    )

    logger.info("Routed %s → dept=%s assignee=%s", map_id, dept_id, assignee)
    return {
        "status": "routed",
        "map_id": map_id,
        "assignee": assignee,
        "department": dept_id,
        "escalation_chain": escalation_chain,
        "auto_escalate_at": auto_escalate_at.isoformat(),
        "notification_sent": True,
    }
