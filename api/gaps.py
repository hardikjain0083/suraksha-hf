from fastapi import APIRouter, HTTPException, Depends, UploadFile, File, Form, status
from typing import List, Dict, Optional, Any
from database import db
from services.gap_detector import detect_gaps_for_circular, recheck_policy_gap
from api.auth import get_current_user
from datetime import datetime
import uuid
import io

router = APIRouter()

def get_db():
    return db.client.suraksha_maps

# Helper: Extract text from file upload
async def extract_text_from_upload(file: UploadFile) -> str:
    file_bytes = await file.read()
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    
    if ext == "pdf":
        from services.watcher import extract_pdf_robust
        res = await extract_pdf_robust(file_bytes)
        return res.text
    elif ext == "docx":
        from docx import Document
        doc = Document(io.BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    else:
        return file_bytes.decode("utf-8", errors="ignore")

# ─────────────────────────────────────────────────────────────
#  Notifications Enpoints
# ─────────────────────────────────────────────────────────────

@router.get("/notifications")
async def get_notifications(current_user: dict = Depends(get_current_user)):
    database = get_db()
    emp_id = current_user["emp_id"]
    
    # Super admins see all notifications, others see their own
    query = {}
    if current_user.get("role") not in ("super_admin", "admin"):
        query["user_id"] = emp_id

    cursor = database.notifications.find(query).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        items.append(item)
    return items

@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(notif_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    result = await database.notifications.update_one(
        {"notification_id": notif_id},
        {"$set": {"is_read": True}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Notification not found")
    return {"status": "success"}

# ─────────────────────────────────────────────────────────────
#  Gap Detection & Ingestion
# ─────────────────────────────────────────────────────────────

@router.post("/detect/{circular_id:path}")
async def run_gap_detection(circular_id: str, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("super_admin", "admin", "compliance_officer"):
        raise HTTPException(status_code=403, detail="Admin permissions required")
    database = get_db()
    try:
        result = await detect_gaps_for_circular(circular_id, database)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gap detection failed: {str(e)}")

@router.get("/circulars")
async def list_circulars_eligible(current_user: dict = Depends(get_current_user)):
    database = get_db()
    cursor = database.circulars.find(
        {"ingestion_status": "fully_parsed"},
        {"circular_id": 1, "title": 1, "issuer": 1, "date_issued": 1}
    ).sort("date_issued", -1)
    
    items = []
    async for c in cursor:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("date_issued"), datetime):
            c["date_issued"] = c["date_issued"].isoformat()
        items.append(c)
    return items

# ─────────────────────────────────────────────────────────────
#  Gap Retrieval Routes
# ─────────────────────────────────────────────────────────────

@router.get("/all")
async def get_all_gaps(current_user: dict = Depends(get_current_user)):
    """Fetch all gaps in the system (segregated by date/time)."""
    database = get_db()
    cursor = database.gap_queue.find({}).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        if isinstance(item.get("due_date"), datetime):
            item["due_date"] = item["due_date"].isoformat()
        items.append(item)
    return items

@router.get("/department/{dept_id}")
async def get_department_gaps(dept_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch gaps assigned to the HOD's department."""
    database = get_db()
    # Resolve HOD role verification
    if current_user.get("role") == "dept_head" and current_user.get("department_id") != dept_id:
        raise HTTPException(status_code=403, detail="Access denied to other departments")
        
    cursor = database.gap_queue.find({"department_id": dept_id}).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        if isinstance(item.get("due_date"), datetime):
            item["due_date"] = item["due_date"].isoformat()
        items.append(item)
    return items

@router.get("/employee/{emp_id}")
async def get_employee_gaps(emp_id: str, current_user: dict = Depends(get_current_user)):
    """Fetch gaps assigned to a specific employee."""
    database = get_db()
    if current_user.get("emp_id") != emp_id and current_user.get("role") not in ("super_admin", "admin", "dept_head"):
        raise HTTPException(status_code=403, detail="Access denied")
        
    cursor = database.gap_queue.find({"assigned_employee": emp_id}).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        if isinstance(item.get("created_at"), datetime):
            item["created_at"] = item["created_at"].isoformat()
        if isinstance(item.get("due_date"), datetime):
            item["due_date"] = item["due_date"].isoformat()
        items.append(item)
    return items

@router.get("/detail/{gap_id}")
async def get_gap_detail(gap_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
    gap["_id"] = str(gap["_id"])
    if isinstance(gap.get("created_at"), datetime):
        gap["created_at"] = gap["created_at"].isoformat()
    if isinstance(gap.get("due_date"), datetime):
        gap["due_date"] = gap["due_date"].isoformat()
    return gap

# ─────────────────────────────────────────────────────────────
#  HOD Actions
# ─────────────────────────────────────────────────────────────

@router.patch("/{gap_id}/cancel")
async def cancel_gap(
    gap_id: str,
    reason: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    # Block HOD from cancelling critical gaps
    if current_user.get("role") == "dept_head" and gap.get("severity") == "critical":
        raise HTTPException(status_code=403, detail="Critical gaps cannot be cancelled by Department Heads. Requires Admin override.")
        
    # Verify permission (HOD of that department or Admin)
    if current_user.get("role") not in ("super_admin", "admin") and (
        current_user.get("role") != "dept_head" or current_user.get("department_id") != gap.get("department_id")
    ):
        raise HTTPException(status_code=403, detail="Action unauthorized")
        
    await database.gap_queue.update_one(
        {"gap_id": gap_id},
        {"$set": {"triage_status": "cancelled", "classification_reason": f"Cancelled by {current_user['role']}. Reason: {reason}"}}
    )
    
    # Notify employee
    await database.notifications.insert_one({
        "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
        "user_id": gap.get("assigned_employee"),
        "title": "Task Cancelled",
        "message": f"Assigned Gap {gap_id} has been cancelled by the department HOD or Admin. Reason: {reason}",
        "type": "info",
        "is_read": False,
        "created_at": datetime.utcnow()
    })
    
    return {"status": "success", "message": "Gap cancelled successfully"}

@router.patch("/{gap_id}/reassign")
async def reassign_gap(
    gap_id: str,
    new_employee_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    # Verify permission (HOD of that department or Admin always hold reassignment power)
    if current_user.get("role") not in ("super_admin", "admin") and (
        current_user.get("role") != "dept_head" or current_user.get("department_id") != gap.get("department_id")
    ):
        raise HTTPException(status_code=403, detail="Action unauthorized")
        
    # Verify new employee exists and is in the same department
    new_emp = await database.users.find_one({"emp_id": new_employee_id})
    if not new_emp or new_emp.get("department_id") != gap.get("department_id"):
        raise HTTPException(status_code=400, detail="New employee must exist and belong to the same department")

    # Availability & Workload checks
    if new_emp.get("availability_status", "available") != "available":
        raise HTTPException(status_code=400, detail=f"Employee is currently {new_emp.get('availability_status')}")
    if new_emp.get("active_gap_count", 0) >= new_emp.get("max_concurrent_gaps", 5):
        raise HTTPException(status_code=400, detail="Employee has reached their maximum workload limit")

    old_employee_id = gap.get("assigned_employee")
    if old_employee_id:
        await database.users.update_one({"emp_id": old_employee_id}, {"$inc": {"active_gap_count": -1}})
        
    await database.users.update_one(
        {"emp_id": new_employee_id},
        {
            "$inc": {"active_gap_count": 1},
            "$set": {"last_assigned_date": datetime.utcnow()}
        }
    )
    
    await database.gap_queue.update_one(
        {"gap_id": gap_id},
        {"$set": {"assigned_employee": new_employee_id, "triage_status": "assigned"}}
    )
    
    # Notify old employee
    if old_employee_id:
        await database.notifications.insert_one({
            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
            "user_id": old_employee_id,
            "title": "Task Reassigned",
            "message": f"Gap {gap_id} has been reassigned to another employee.",
            "type": "info",
            "is_read": False,
            "created_at": datetime.utcnow()
        })
    
    # Notify new employee
    await database.notifications.insert_one({
        "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
        "user_id": new_employee_id,
        "title": "New Compliance Task Assigned",
        "message": f"Gap {gap_id} has been reassigned to you. Please check dashboard.",
        "type": "assigned",
        "is_read": False,
        "created_at": datetime.utcnow()
    })
    
    return {"status": "success", "message": f"Gap reassigned successfully to {new_emp.get('name')}"}

@router.patch("/{gap_id}/edit")
async def edit_gap(
    gap_id: str,
    mismatch_description: str = Form(None),
    due_date: str = Form(None),
    severity: str = Form(None),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    # Verify permission
    if current_user.get("role") not in ("super_admin", "admin") and (
        current_user.get("role") != "dept_head" or current_user.get("department_id") != gap.get("department_id")
    ):
        raise HTTPException(status_code=403, detail="Action unauthorized")
        
    update_data = {}
    if mismatch_description:
        update_data["mismatch_description"] = mismatch_description
        update_data["classification_reason"] = mismatch_description
    if due_date:
        update_data["due_date"] = datetime.fromisoformat(due_date.replace("Z", ""))
    if severity:
        update_data["severity"] = severity
        
    if update_data:
        await database.gap_queue.update_one({"gap_id": gap_id}, {"$set": update_data})
        
        # Notify employee
        await database.notifications.insert_one({
            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
            "user_id": gap.get("assigned_employee"),
            "title": "Task Details Updated",
            "message": f"Details for Gap {gap_id} have been updated by your department HOD.",
            "type": "info",
            "is_read": False,
            "created_at": datetime.utcnow()
        })
        
    return {"status": "success", "message": "Gap details updated successfully"}

# ─────────────────────────────────────────────────────────────
#  Employee Action: Submit Fix
# ─────────────────────────────────────────────────────────────

@router.post("/{gap_id}/submit-fix")
async def submit_gap_fix(
    gap_id: str,
    file: UploadFile = File(None),
    updated_text: str = Form(None),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    # Verify employee assignment
    if current_user.get("emp_id") != gap.get("assigned_employee"):
        raise HTTPException(status_code=403, detail="This task is not assigned to you")
        
    # Resolve revised policy content
    content_text = ""
    if file:
        content_text = await extract_text_from_upload(file)
    elif updated_text:
        content_text = updated_text
    else:
        raise HTTPException(status_code=400, detail="Must provide either updated policy file or updated text content")
        
    if not content_text.strip():
        raise HTTPException(status_code=400, detail="Extracted policy text content is empty")

    # Run re-checking locally
    res = await recheck_policy_gap(gap_id, content_text, database)
    
    return {
        "status": "checked",
        "resolved": res["resolved"],
        "remaining_gaps": res.get("remaining_gaps", []),
        "regressions": res.get("new_gaps", []),
        "result_status": res.get("status", "failed")
    }

# ─────────────────────────────────────────────────────────────
#  Admin Action: Approve Fix & Update Core Policy
# ─────────────────────────────────────────────────────────────

@router.post("/{gap_id}/approve-fix")
async def approve_gap_fix(gap_id: str, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Super Admin permissions required")
        
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    if not gap.get("is_fixed") or gap.get("triage_status") != "resolved":
        raise HTTPException(status_code=400, detail="This gap has not been successfully resolved by the employee yet")
        
    policy_id = gap.get("top_policy_id")
    fixed_content = gap.get("fixed_policy_content")
    
    # Retrieve the active policy
    policy = await database.policies.find_one({"policy_id": policy_id})
    if not policy:
        raise HTTPException(status_code=404, detail="Core policy not found")
        
    # Generate embedding for updated content
    from services.watcher import generate_embeddings
    try:
        embs = await generate_embeddings([fixed_content])
        embedding = embs[0] if embs else []
    except Exception:
        embedding = []

    # Update core policy
    await database.policies.update_one(
        {"policy_id": policy_id},
        {
            "$set": {
                "content": fixed_content,
                "full_text": fixed_content,
                "embedding": embedding,
                "updated_at": datetime.utcnow()
            }
        }
    )
    
    # Notify HOD and Employee of final integration
    await database.notifications.insert_one({
        "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
        "user_id": gap.get("assigned_employee"),
        "title": "Policy Changes Applied",
        "message": f"Your compliance revisions for Gap {gap_id} have been officially approved and merged into core policy.",
        "type": "info",
        "is_read": False,
        "created_at": datetime.utcnow()
    })
    
    await database.notifications.insert_one({
        "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
        "user_id": gap.get("assigned_hod"),
        "title": "Core Policy Updated",
        "message": f"Approved compliance revisions for Gap {gap_id} in Policy '{gap.get('top_policy_title')}' have been integrated.",
        "type": "info",
        "is_read": False,
        "created_at": datetime.utcnow()
    })
    
    return {"status": "success", "message": "Core policy updated successfully with revised version"}

# ─────────────────────────────────────────────────────────────
#  Hacking Hardening Endpoints
# ─────────────────────────────────────────────────────────────

@router.get("/orphaned")
async def get_orphaned_directives(current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    database = get_db()
    cursor = database.gap_queue.find({"gap_status": "confirmed", "routing": "orphaned_directive"}).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        items.append(item)
    return items

@router.get("/ambiguous")
async def get_ambiguous_gaps(current_user: dict = Depends(get_current_user)):
    database = get_db()
    query = {"is_ambiguous": True}
    if current_user.get("role") == "dept_head":
        query["ambiguous_departments"] = current_user.get("department_id")
    elif current_user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Access denied")
        
    cursor = database.gap_queue.find(query).sort("created_at", -1)
    items = []
    async for item in cursor:
        item["_id"] = str(item["_id"])
        items.append(item)
    return items

@router.post("/{gap_id}/resolve-ambiguous")
async def resolve_ambiguous_gap(
    gap_id: str,
    chosen_department_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    if current_user.get("role") not in ("super_admin", "admin"):
        if current_user.get("role") == "dept_head" and current_user.get("department_id") in gap.get("ambiguous_departments", []):
            pass
        else:
            raise HTTPException(status_code=403, detail="Unauthorized to resolve ambiguity for this gap")
            
    dept_doc = await database.departments.find_one({"department_id": chosen_department_id})
    if not dept_doc:
        raise HTTPException(status_code=400, detail="Department does not exist")
        
    hod_id = dept_doc.get("head_employee_id", "EMP-COMPLIANCE-HEAD")
    
    from services.gap_detector import get_available_employee
    employee_id = await get_available_employee(database, chosen_department_id)
    
    await database.gap_queue.update_one(
        {"gap_id": gap_id},
        {
            "$set": {
                "department_id": chosen_department_id,
                "assigned_hod": hod_id,
                "assigned_employee": employee_id,
                "is_ambiguous": False,
                "triage_status": "assigned" if employee_id else "open"
            }
        }
    )
    
    if employee_id:
        await database.users.update_one(
            {"emp_id": employee_id},
            {
                "$inc": {"active_gap_count": 1},
                "$set": {"last_assigned_date": datetime.utcnow()}
            }
        )
        
    return {"status": "success", "message": f"Gap successfully routed to department {chosen_department_id}"}

@router.put("/admin/circulars/{gap_id}/resolve-orphan")
async def resolve_orphaned_directive(
    gap_id: str,
    department_id: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user.get("role") not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="Admin privileges required")
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    dept_doc = await database.departments.find_one({"department_id": department_id})
    if not dept_doc:
        raise HTTPException(status_code=400, detail="Department does not exist")
        
    hod_id = dept_doc.get("head_employee_id", "EMP-COMPLIANCE-HEAD")
    from services.gap_detector import get_available_employee
    employee_id = await get_available_employee(database, department_id)
    
    await database.gap_queue.update_one(
        {"gap_id": gap_id},
        {
            "$set": {
                "department_id": department_id,
                "assigned_hod": hod_id,
                "assigned_employee": employee_id,
                "routing": "auto_routed",
                "triage_status": "assigned" if employee_id else "open"
            }
        }
    )
    
    if employee_id:
        await database.users.update_one(
            {"emp_id": employee_id},
            {
                "$inc": {"active_gap_count": 1},
                "$set": {"last_assigned_date": datetime.utcnow()}
            }
        )
        
    return {"status": "success", "message": f"Orphaned directive routed to department {department_id}"}

@router.post("/bulk-assign")
async def bulk_assign_gaps(
    gap_ids: List[str],
    hod_id: str = Form(...),
    employee_id: str = Form(None),
    current_user: dict = Depends(get_current_user)
):
    if current_user.get("role") not in ("super_admin", "admin", "dept_head"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    database = get_db()
    
    hod_user = await database.users.find_one({"emp_id": hod_id, "role": "dept_head"})
    if not hod_user:
        raise HTTPException(status_code=400, detail="Target HOD not found")
    dept_id = hod_user["department_id"]
    
    if employee_id:
        emp_user = await database.users.find_one({"emp_id": employee_id})
        if not emp_user or emp_user.get("department_id") != dept_id:
            raise HTTPException(status_code=400, detail="Employee must belong to HOD's department")
            
    updated_count = 0
    for gid in gap_ids:
        gap = await database.gap_queue.find_one({"gap_id": gid})
        if gap:
            old_emp = gap.get("assigned_employee")
            if old_emp:
                await database.users.update_one({"emp_id": old_emp}, {"$inc": {"active_gap_count": -1}})
            
            update_set = {
                "assigned_hod": hod_id,
                "department_id": dept_id,
                "assigned_employee": employee_id,
                "triage_status": "assigned" if employee_id else "open"
            }
            await database.gap_queue.update_one({"gap_id": gid}, {"$set": update_set})
            
            if employee_id:
                await database.users.update_one(
                    {"emp_id": employee_id},
                    {
                        "$inc": {"active_gap_count": 1},
                        "$set": {"last_assigned_date": datetime.utcnow()}
                    }
                )
            updated_count += 1
            
    return {"status": "success", "message": f"Successfully reassigned {updated_count} gaps"}

@router.post("/bulk-severity")
async def bulk_severity_gaps(
    gap_ids: List[str],
    severity: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    if current_user.get("role") not in ("super_admin", "admin", "dept_head"):
        raise HTTPException(status_code=403, detail="Unauthorized")
    database = get_db()
    
    if current_user.get("role") == "dept_head" and severity == "critical":
        raise HTTPException(status_code=403, detail="HODs cannot bulk-elevate gaps to CRITICAL")
        
    res = await database.gap_queue.update_many(
        {"gap_id": {"$in": gap_ids}},
        {"$set": {"severity": severity}}
    )
    return {"status": "success", "message": f"Updated severity for {res.modified_count} gaps"}

@router.put("/employee/availability")
async def update_employee_availability(
    availability_status: str = Form(...),
    employee_id: str = Form(None),
    current_user: dict = Depends(get_current_user)
):
    if availability_status not in ("available", "on_leave", "busy"):
        raise HTTPException(status_code=400, detail="Invalid availability status")
    
    target_emp_id = current_user["emp_id"]
    if employee_id and employee_id != current_user["emp_id"]:
        if current_user.get("role") not in ("super_admin", "admin", "dept_head"):
            raise HTTPException(status_code=403, detail="Unauthorized to update other user's availability")
        target_emp_id = employee_id

    database = get_db()
    await database.users.update_one(
        {"emp_id": target_emp_id},
        {"$set": {"availability_status": availability_status}}
    )
    return {"status": "success", "message": f"Availability updated to {availability_status}"}

@router.post("/employee/gaps/{gap_id}/check-regression")
async def check_regression_pre_submit(
    gap_id: str,
    updated_text: str = Form(...),
    current_user: dict = Depends(get_current_user)
):
    database = get_db()
    gap = await database.gap_queue.find_one({"gap_id": gap_id})
    if not gap:
        raise HTTPException(status_code=404, detail="Gap not found")
        
    clause_text = gap.get("clause_text", "")
    
    from services.gap_detector import RakeKeywordExtractor, calculate_weighted_score, SIMILARITY_THRESHOLD_MATCH, SIMILARITY_THRESHOLD_PARTIAL
    rake = RakeKeywordExtractor()
    
    clause_kws = set(rake.extract_keywords(clause_text))
    updated_kws = set(rake.extract_keywords(updated_text))
    
    score = calculate_weighted_score(clause_text, updated_text, clause_kws, updated_kws)
    original_resolved = score >= SIMILARITY_THRESHOLD_MATCH
    
    active_circulars = await database.circulars.find({"ingestion_status": {"$in": ["fully_parsed", "partially_parsed"]}}).to_list(length=100)
    regressions = []
    for circ in active_circulars:
        circ_clauses = circ.get("clauses", [])
        for cc in circ_clauses:
            if cc.get("obligation_type") not in ("shall", "must", "should"):
                continue
            cc_text = cc.get("text", "")
            if cc_text == clause_text:
                continue
            cc_kws = set(rake.extract_keywords(cc_text))
            cc_score = calculate_weighted_score(cc_text, updated_text, cc_kws, updated_kws)
            if cc_score < SIMILARITY_THRESHOLD_PARTIAL:
                regressions.append({
                    "circular_title": circ.get("title", ""),
                    "clause_text": cc_text,
                    "score": cc_score
                })
                
    return {
        "original_resolved": original_resolved,
        "original_score": score,
        "has_regressions": len(regressions) > 0,
        "regressions": regressions
    }

@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    task = await database.processing_tasks.find_one({"id": task_id})
    if not task:
        return {"status": "completed", "total_items": 1, "processed_items": 1, "progress_percentage": 100}
    task["_id"] = str(task["_id"])
    return task
