import re
import uuid
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from models.map import ProvenanceNode, EvidenceItem, AuditTrailEntry, TimelineEntry, SimilarMapRef

logger = logging.getLogger(__name__)

DEFAULT_DEADLINE_DAYS = 45

# Dynamic SLA deadlines used instead of a fixed default

CATEGORY_DEPARTMENT = {
    "cryptography": "DEPT-INFOSEC",
    "authentication": "DEPT-IT",
    "data_protection": "DEPT-INFOSEC",
    "network_security": "DEPT-INFOSEC",
    "monitoring": "DEPT-IT",
    "audit": "DEPT-COMPLIANCE",
    "compliance": "DEPT-LEGAL",
    "legal": "DEPT-LEGAL",
    "cybersecurity": "DEPT-INFOSEC",
    "default": "DEPT-COMPLIANCE",
}

EVIDENCE_BY_CATEGORY = {
    "cryptography": [
        ("config_file", "Configuration export"),
        ("screenshot", "Encryption settings screenshot"),
    ],
    "authentication": [
        ("config_file", "IAM/MFA configuration"),
        ("screenshot", "MFA enrollment dashboard"),
    ],
    "audit": [
        ("pdf_document", "Audit report PDF"),
        ("scan_report", "Vulnerability scan report"),
    ],
    "monitoring": [
        ("screenshot", "SIEM dashboard"),
        ("config_file", "Alert rule configuration"),
    ],
    "process": [
        ("pdf_document", "Signed policy document"),
        ("ticket_id", "Change management ticket"),
    ],
    "default": [
        ("pdf_document", "Compliance evidence document"),
        ("ticket_id", "Remediation ticket ID"),
    ],
}

SEVERITY_BASE = {"critical": 100, "high": 75, "medium": 50, "low": 25}
ISSUER_BOOST = {"RBI": 15, "SEBI": 10, "CERT-In": 10, "CERT-IN": 10}


def _detect_clause_category(clause_text: str) -> str:
    text = clause_text.lower()
    if any(k in text for k in ("encrypt", "aes", "tls", "cipher", "cryptograph")):
        return "cryptography"
    if any(k in text for k in ("mfa", "multi-factor", "authenticate", "password", "sso")):
        return "authentication"
    if any(k in text for k in ("audit", "log", "siem", "monitor")):
        return "audit" if "audit" in text else "monitoring"
    if any(k in text for k in ("comply", "regulation", "statutory", "legal")):
        return "compliance"
    if any(k in text for k in ("firewall", "vpn", "network", "perimeter")):
        return "network_security"
    return "default"


def generate_title_from_clause(clause_text: str) -> str:
    """Extract action verb + object → title case MAP title."""
    text = clause_text.strip()
    match = re.search(
        r"\b(shall|must|will|should)\s+(.+?)(?:\.|,|;|$)",
        text,
        re.IGNORECASE,
    )
    action = match.group(2).strip() if match else text[:120]
    action = re.sub(r"\s+", " ", action)
    if len(action) > 100:
        action = action[:97] + "..."
    return action[:1].upper() + action[1:] if action else "Remediate Compliance Gap"


def generate_description(clause_text: str, circular_title: str, policy_title: Optional[str]) -> str:
    ctx = f"Per regulatory circular '{circular_title}'"
    if policy_title:
        ctx += f", aligned against internal policy '{policy_title}'"
    return (
        f"{ctx}, the institution must ensure compliance with the following obligation: "
        f"{clause_text.strip()} "
        "Documented evidence of implementation must be submitted before the MAP deadline."
    )


def suggest_evidence_types(category: str) -> list[dict[str, str]]:
    pairs = EVIDENCE_BY_CATEGORY.get(category, EVIDENCE_BY_CATEGORY["default"])
    if category not in ("cryptography", "authentication", "audit", "monitoring"):
        pairs = EVIDENCE_BY_CATEGORY["process"]
    return [{"type": t, "label": l} for t, l in pairs]


def build_evidence_items(suggested: list[dict[str, str]]) -> list[dict]:
    return [
        {
            "evidence_type": s["type"],
            "label": s["label"],
            "required": True,
            "uploaded": False,
        }
        for s in suggested
    ]


def calculate_priority_score(
    severity: str,
    deadline: datetime,
    issuer: Optional[str],
) -> int:
    base = SEVERITY_BASE.get((severity or "medium").lower(), 50)
    days_left = (deadline - datetime.utcnow()).days
    if days_left < 7:
        base += 20
    elif days_left < 14:
        base += 10
    if issuer:
        base += ISSUER_BOOST.get(issuer.upper(), ISSUER_BOOST.get(issuer, 0))
    return min(100, base)


def severity_to_risk(severity: Optional[str], gap_status: str) -> str:
    if severity:
        return severity.lower() if severity.lower() in SEVERITY_BASE else "medium"
    if gap_status == "confirmed":
        return "high"
    if gap_status == "suspected":
        return "medium"
    return "low"


async def resolve_department_graph_lookup(
    database: Any,
    gap: dict,
    policy_id: Optional[str],
    category: str,
) -> tuple[str, str]:
    """
    MongoDB aggregation: gap → policy → department (with $graphLookup on dept hierarchy).
    """
    dept_name = "Compliance"
    dept_id = CATEGORY_DEPARTMENT.get(category, CATEGORY_DEPARTMENT["default"])

    if policy_id:
        pipeline = [
            {"$match": {"policy_id": policy_id}},
            {
                "$lookup": {
                    "from": "departments",
                    "localField": "department_owner_id",
                    "foreignField": "department_id",
                    "as": "dept",
                }
            },
            {"$unwind": {"path": "$dept", "preserveNullAndEmptyArrays": True}},
            {
                "$graphLookup": {
                    "from": "departments",
                    "startWith": "$dept.parent_department_id",
                    "connectFromField": "parent_department_id",
                    "connectToField": "department_id",
                    "as": "dept_chain",
                    "maxDepth": 2,
                }
            },
            {
                "$project": {
                    "department_owner_id": 1,
                    "dept_name": "$dept.name",
                    "dept_id": "$dept.department_id",
                }
            },
        ]
        async for row in database.policies.aggregate(pipeline):
            if row.get("department_owner_id"):
                dept_id = row["department_owner_id"]
            if row.get("dept_name"):
                dept_name = row["dept_name"]
            elif row.get("dept_id"):
                dept_id = row["dept_id"]
            break
    else:
        dept = await database.departments.find_one({"department_id": dept_id})
        if dept:
            dept_name = dept.get("name", dept_name)

    return dept_id, dept_name


async def find_similar_historical_maps(
    database: Any,
    clause_text: str,
    limit: int = 5,
) -> tuple[list[SimilarMapRef], int]:
    """Count and fetch resolved MAPs with similar clause obligations."""
    keywords = [w.lower() for w in re.findall(r"\b[A-Za-z]{5,}\b", clause_text)[:8]]
    if not keywords:
        return [], 0

    regex = "|".join(re.escape(k) for k in keywords[:5])
    query = {
        "status": {"$in": ["complete", "published", "approved", "open", "in_progress"]},
        "$or": [
            {"clause_text": {"$regex": regex, "$options": "i"}},
            {"description": {"$regex": regex, "$options": "i"}},
            {"title": {"$regex": regex, "$options": "i"}},
        ],
    }
    count = await database.maps.count_documents(query)
    cursor = database.maps.find(query).sort("created_at", -1).limit(limit)
    refs = []
    async for m in cursor:
        refs.append(
            SimilarMapRef(
                map_id=m["map_id"],
                title=m.get("title", m.get("action_plan", "MAP")[:80]),
                status=m.get("status", "complete"),
                resolved_at=m.get("completed_at") or m.get("approved_at"),
            )
        )
    return refs, count


async def get_circular_context(database: Any, circular_id: str, clause_number: Optional[str]) -> dict:
    circular = await database.circulars.find_one({"circular_id": circular_id})
    if not circular:
        return {"title": circular_id, "issuer": "UNKNOWN", "clause_text": "", "clause_id": clause_number or ""}

    clause_text = ""
    clause_id = clause_number or ""
    for c in circular.get("clauses", []):
        cn = c.get("clause_number") or c.get("clause_id", "")
        if clause_number and str(cn) == str(clause_number):
            clause_text = c.get("text", "")
            clause_id = str(cn)
            break
    if not clause_text and circular.get("clauses"):
        clause_text = circular["clauses"][0].get("text", "")
        clause_id = circular["clauses"][0].get("clause_number") or circular["clauses"][0].get("clause_id", "")

    return {
        "title": circular.get("title", circular_id),
        "issuer": circular.get("issuer", "UNKNOWN"),
        "clause_text": clause_text,
        "clause_id": clause_id,
    }


def build_provenance_path(
    circular_id: str,
    circular_title: str,
    clause_id: str,
    clause_text: str,
    gap_id: str,
    gap_classification: str,
    policy_id: Optional[str],
    policy_title: Optional[str],
    dept_id: str,
    dept_name: str,
) -> list[dict]:
    return [
        {"type": "circular", "id": circular_id, "title": circular_title},
        {"type": "clause", "id": clause_id, "text": clause_text[:500]},
        {"type": "gap", "id": gap_id, "classification": gap_classification},
        {"type": "policy", "id": policy_id or "none", "title": policy_title},
        {"type": "department", "id": dept_id, "name": dept_name},
    ]


def build_timeline(status: str, created_at: datetime, approved_at: Optional[datetime] = None) -> list[dict]:
    stages = [
        ("created", "Created", True),
        ("approved", "Approved", status not in ("draft", "rejected")),
        ("open", "Open", status in ("open", "in_progress", "complete", "published", "approved")),
        ("in_progress", "In Progress", status in ("in_progress", "complete", "published")),
        ("complete", "Complete", status in ("complete", "published")),
    ]
    timeline = []
    for key, label, done in stages:
        ts = created_at if key == "created" else approved_at if key == "approved" and approved_at else None
        timeline.append({"stage": key, "label": label, "timestamp": ts, "completed": done})
    return timeline


async def generate_map_from_gap(
    database: Any,
    gap_id: str,
    officer_id: str = "system",
    officer_name: str = "Compliance Officer",
) -> dict:
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise ValueError(f"Gap not found: {gap_id}")

    existing = await database.maps.find_one({"gap_id": gap_id, "status": {"$ne": "rejected"}})
    if existing:
        return existing

    circular_ctx = await get_circular_context(
        database, gap["circular_id"], gap.get("clause_number")
    )
    clause_text = gap.get("clause_text") or circular_ctx["clause_text"]
    policy_id = gap.get("top_policy_id")
    policy_title = gap.get("top_policy_title")

    title = generate_title_from_clause(clause_text)
    description = generate_description(clause_text, circular_ctx["title"], policy_title)

    # SLA-driven dynamic deadlines
    sev_lower = (gap.get("severity") or "medium").lower()
    if sev_lower == "critical":
        sla_days = 3
    elif sev_lower == "high":
        sla_days = 7
    elif sev_lower == "medium":
        sla_days = 15
    else:
        sla_days = 45

    deadline = datetime.utcnow() + timedelta(days=sla_days)

    similar_refs, hist_count = await find_similar_historical_maps(database, clause_text)
    if gap.get("historical_match_count") is not None:
        hist_count = max(hist_count, gap["historical_match_count"])

    priority = calculate_priority_score(
        gap.get("severity", "medium"), deadline, circular_ctx.get("issuer")
    )

    # Fetch circular content for prompt context
    circ_doc = await database.circulars.find_one({"circular_id": gap["circular_id"]})
    circ_content = circ_doc.get("content", "") if circ_doc else ""

    from services.ai_service import classify_department, select_employee_for_map, generate_evidence_template
    from services.audit_logger import append_audit_log

    # 1. Classify Department using Gemini Classification Agent
    class_res = await classify_department(
        gap_description=gap.get("gap_description") or clause_text,
        circular_title=circular_ctx["title"],
        circular_content=circ_content
    )

    dept_id = class_res["department_id"]
    confidence = class_res["confidence_score"]
    reasoning = class_res["reasoning"]
    multi_dept = class_res["multi_department"]
    affected_depts = class_res["affected_departments"]

    # If confidence score < 0.80, set status to pending_admin_assignment
    is_low_conf = confidence < 0.80

    # Get department details
    dept = await database.departments.find_one({"department_id": dept_id})
    dept_name = dept.get("name", "Compliance") if dept else "Compliance"

    # Common fields for map document
    category = _detect_clause_category(clause_text)
    suggested = suggest_evidence_types(category)

    def get_file_type(s_type: str) -> str:
        s_lower = s_type.lower()
        if "config" in s_lower or "file" in s_lower or "document" in s_lower:
            return "docx"
        elif "screenshot" in s_lower or "image" in s_lower:
            return "image"
        return "pdf"

    def build_custom_evidence_items():
        items = []
        for s in suggested:
            items.append({
                "evidence_id": f"ev_{uuid.uuid4().hex[:10]}",
                "file_url": "",
                "file_type": get_file_type(s["type"]),
                "uploaded_at": None,
                "uploaded_by": "",
                "description": s["label"],
                "validation_status": "pending",
                "validation_notes": ""
            })
        return items

    provenance = build_provenance_path(
        gap["circular_id"],
        circular_ctx["title"],
        str(gap.get("clause_number") or circular_ctx["clause_id"]),
        clause_text,
        gap_id,
        gap.get("gap_status", "confirmed"),
        policy_id,
        policy_title,
        dept_id,
        dept_name,
    )

    now = datetime.utcnow()
    risk = severity_to_risk(gap.get("severity"), gap.get("gap_status", "confirmed"))

    requirements = [
        f"Implement controls per clause {gap.get('clause_number', 'N/A')}",
        "Submit all required evidence types before deadline",
        f"Coordinate with {dept_name} for sign-off",
    ]

    base_map_doc = {
        "gap_id": gap_id,
        "circular_id": gap["circular_id"],
        "policy_id": policy_id or "UNASSIGNED",
        "title": title,
        "description": description,
        "requirements": requirements,
        "deadline": deadline,
        "priority_score": priority,
        "risk_level": risk,
        "clause_text": clause_text,
        "historical_match_count": hist_count,
        "similar_past_maps": [r.model_dump() for r in similar_refs],
        "created_at": now,
        "issuer": circular_ctx.get("issuer"),
        "routing_source": gap.get("routing", "pending_review"),
        "provenance_path": provenance,
    }

    created_maps = []

    # If low confidence
    if is_low_conf:
        # Create single MAP for manual admin triage
        map_id = f"MAP-{uuid.uuid4().hex[:8].upper()}"
        map_doc = {
            **base_map_doc,
            "map_id": map_id,
            "master_map_id": None,
            "owner_department_id": dept_id,
            "department_name": dept_name,
            "assigned_to": None,
            "assigned_by_ai": False,
            "assignment_confidence": confidence,
            "assignment_reason": f"AI flagged low confidence: {reasoning}",
            "ai_flagged_low_confidence": True,
            "status": "pending_admin_assignment",
            "evidence_items": build_custom_evidence_items(),
            "audit_trail": [{
                "timestamp": now,
                "user_id": officer_id,
                "user_name": officer_name,
                "action": "ai_flagged_low_confidence",
                "details": f"Flagged for admin manual assignment due to low confidence ({confidence:.2f})"
            }],
            "timeline": build_timeline("pending_admin_assignment", now)
        }
        await database.maps.insert_one(map_doc)
        created_maps.append(map_doc)
        
        # Log audit entry
        await append_audit_log(
            database,
            action_type="ai_low_confidence_flagged",
            target_type="map",
            target_id=map_id,
            user_id="system",
            user_name="AI Classification Agent",
            details={"confidence": confidence, "reasoning": reasoning}
        )
    else:
        # Check if cross-department MAP
        if multi_dept and affected_depts:
            # Create Master MAP first
            master_map_id = f"MMAP-{uuid.uuid4().hex[:8].upper()}"
            await database.master_maps.insert_one({
                "master_map_id": master_map_id,
                "circular_id": gap["circular_id"],
                "title": f"Master MAP: {title}",
                "overall_status": "open",
                "linked_circular_ids": [gap["circular_id"]],
                "created_at": now
            })

            # Create Sub-MAP for main department
            main_sub_id = f"MAP-{uuid.uuid4().hex[:8].upper()}"
            main_assignee, main_assign_reason = await select_employee_for_map(
                database=database,
                department_id=dept_id,
                gap_description=clause_text,
                gap_tags=[category]
            )
            
            # If no employee found, set status to pending_admin_assignment
            main_status = "pending_head_review" if main_assignee else "pending_admin_assignment"
            main_evidence = build_custom_evidence_items()
            
            main_sub_doc = {
                **base_map_doc,
                "map_id": main_sub_id,
                "master_map_id": master_map_id,
                "owner_department_id": dept_id,
                "department_name": dept_name,
                "assigned_to": main_assignee,
                "assigned_by_ai": True,
                "assignment_confidence": confidence,
                "assignment_reason": main_assign_reason,
                "ai_flagged_low_confidence": False,
                "status": main_status,
                "evidence_items": main_evidence,
                "audit_trail": [{
                    "timestamp": now,
                    "user_id": officer_id,
                    "user_name": officer_name,
                    "action": "sub_map_created",
                    "details": f"Sub-MAP created under master {master_map_id}. Route assignee: {main_assignee}"
                }],
                "timeline": build_timeline(main_status, now)
            }
            await database.maps.insert_one(main_sub_doc)
            created_maps.append(main_sub_doc)

            # Generate and insert evidence template
            if main_assignee:
                tpl_content = await generate_evidence_template(category, clause_text)
                await database.evidence_templates.insert_one({
                    "template_id": f"TPL-{uuid.uuid4().hex[:8].upper()}",
                    "map_id": main_sub_id,
                    "gap_type": category,
                    "template_content": tpl_content,
                    "generated_by_ai": True,
                    "created_at": now
                })

            # Create Sub-MAPs for affected departments
            for affected in affected_depts:
                aff_dept_id = affected["department_id"]
                aff_dept = await database.departments.find_one({"department_id": aff_dept_id})
                aff_dept_name = aff_dept.get("name", "Department") if aff_dept else "Department"

                sub_map_id = f"MAP-{uuid.uuid4().hex[:8].upper()}"
                aff_assignee, aff_assign_reason = await select_employee_for_map(
                    database=database,
                    department_id=aff_dept_id,
                    gap_description=clause_text,
                    gap_tags=[category]
                )
                
                aff_status = "pending_head_review" if aff_assignee else "pending_admin_assignment"
                
                sub_doc = {
                    **base_map_doc,
                    "map_id": sub_map_id,
                    "master_map_id": master_map_id,
                    "owner_department_id": aff_dept_id,
                    "department_name": aff_dept_name,
                    "assigned_to": aff_assignee,
                    "assigned_by_ai": True,
                    "assignment_confidence": confidence,
                    "assignment_reason": aff_assign_reason,
                    "ai_flagged_low_confidence": False,
                    "status": aff_status,
                    "evidence_items": build_custom_evidence_items(),
                    "audit_trail": [{
                        "timestamp": now,
                        "user_id": officer_id,
                        "user_name": officer_name,
                        "action": "sub_map_created",
                        "details": f"Sub-MAP created under master {master_map_id} for department {aff_dept_name}."
                    }],
                    "timeline": build_timeline(aff_status, now)
                }
                await database.maps.insert_one(sub_doc)
                
                if aff_assignee:
                    tpl_content = await generate_evidence_template(category, clause_text)
                    await database.evidence_templates.insert_one({
                        "template_id": f"TPL-{uuid.uuid4().hex[:8].upper()}",
                        "map_id": sub_map_id,
                        "gap_type": category,
                        "template_content": tpl_content,
                        "generated_by_ai": True,
                        "created_at": now
                    })
        else:
            # Create single normal MAP
            map_id = f"MAP-{uuid.uuid4().hex[:8].upper()}"
            assignee, assign_reason = await select_employee_for_map(
                database=database,
                department_id=dept_id,
                gap_description=clause_text,
                gap_tags=[category]
            )
            
            map_status = "pending_head_review" if assignee else "pending_admin_assignment"
            
            map_doc = {
                **base_map_doc,
                "map_id": map_id,
                "master_map_id": None,
                "owner_department_id": dept_id,
                "department_name": dept_name,
                "assigned_to": assignee,
                "assigned_by_ai": True,
                "assignment_confidence": confidence,
                "assignment_reason": assign_reason,
                "ai_flagged_low_confidence": False,
                "status": map_status,
                "evidence_items": build_custom_evidence_items(),
                "audit_trail": [{
                    "timestamp": now,
                    "user_id": officer_id,
                    "user_name": officer_name,
                    "action": "map_generated",
                    "details": f"Generated from gap {gap_id}. Assigned employee: {assignee}"
                }],
                "timeline": build_timeline(map_status, now)
            }
            await database.maps.insert_one(map_doc)
            created_maps.append(map_doc)

            # Generate evidence template
            if assignee:
                tpl_content = await generate_evidence_template(category, clause_text)
                await database.evidence_templates.insert_one({
                    "template_id": f"TPL-{uuid.uuid4().hex[:8].upper()}",
                    "map_id": map_id,
                    "gap_type": category,
                    "template_content": tpl_content,
                    "generated_by_ai": True,
                    "created_at": now
                })

    # Update Gap Queue triage status
    if created_maps:
        await database.gap_queue.update_one(
            {"gap_id": gap_id},
            {"$set": {"generated_map_id": created_maps[0]["map_id"], "triage_status": "assigned"}}
        )

        # Insert into triage actions
        await database.triage_actions.insert_one({
            "action_id": f"ACT-{uuid.uuid4().hex[:8].upper()}",
            "map_id": created_maps[0]["map_id"],
            "gap_id": gap_id,
            "timestamp": now,
            "officer_id": officer_id,
            "officer_name": officer_name,
            "decision": "assigned",
            "title": title,
        })

        return created_maps[0]
    else:
        raise ValueError(f"No MAP could be generated for gap: {gap_id}")



async def log_audit(database: Any, map_id: str, user_id: str, user_name: str, action: str, details: str = ""):
    from services.audit_logger import append_audit_log

    entry = {
        "timestamp": datetime.utcnow(),
        "user_id": user_id,
        "user_name": user_name,
        "action": action,
        "details": details,
    }
    await database.maps.update_one({"map_id": map_id}, {"$push": {"audit_trail": entry}})
    await append_audit_log(
        database,
        action_type=action,
        target_type="map",
        target_id=map_id,
        user_id=user_id,
        user_name=user_name,
        details={"message": details},
        provenance={"map_id": map_id},
    )
    await database.triage_actions.insert_one({
        "action_id": f"ACT-{uuid.uuid4().hex[:8].upper()}",
        "map_id": map_id,
        "timestamp": entry["timestamp"],
        "officer_id": user_id,
        "officer_name": user_name,
        "decision": action.replace("map_", ""),
        "title": details[:80] if details else None,
    })
