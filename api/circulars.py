from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import Response
from database import db
from datetime import datetime
import hashlib
import os

from models.circular import CircularResponse
from services.watcher import process_circular
from services.gridfs_service import upload_file_to_gridfs, download_file_from_gridfs, delete_file_from_gridfs
from services.circular_ids import generate_circular_id
from services.gap_detector import detect_gaps_for_circular
from api.auth import get_current_user

router = APIRouter()
INGESTION_ROLES = {"super_admin", "admin", "compliance_officer"}

def get_db():
    return db.client.suraksha_maps

@router.post("/upload", response_model=dict)
async def upload_circular(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload RBI circular, parse clauses page-by-page, and auto-detect gaps against policies."""
    if current_user.get("role") not in INGESTION_ROLES:
        raise HTTPException(403, "Circular ingestion access required")
    
    database = get_db()
    file_bytes = await file.read()
    
    # Check duplicate circular file
    preview_hash = hashlib.sha256(file_bytes).hexdigest()
    existing = await database.circulars.find_one({"raw_bytes_hash": preview_hash})
    if existing:
        return {
            "status": "duplicate",
            "circular_id": existing["circular_id"],
            "ingestion_status": existing.get("ingestion_status"),
            "message": "Circular with identical content already exists"
        }

    # Upload to gridfs
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    content_type = file.content_type or "application/octet-stream"
    gridfs_id = await upload_file_to_gridfs(file.filename, content_type, file_bytes)

    # Process and parse clauses (page-by-page)
    (
        status, 
        clauses, 
        duration_ms, 
        confidence, 
        full_text, 
        ocr_metadata, 
        flagged_for_manual_review,
        intent,
        is_master_circular,
        supersedes_circulars,
        applicability_conditions,
        structured_blocks
    ) = await process_circular(file_bytes, file.filename)

    # Detect issuer (defaults to RBI for this banking environment)
    issuer = "RBI"
    lower_text = full_text.lower()
    if "sebi" in file.filename.lower() or "securities and exchange" in lower_text:
        issuer = "SEBI"
    
    year = datetime.utcnow().year
    circular_id = await generate_circular_id(database, issuer, year)

    # Extract circular number
    from services.watcher import extract_circular_number
    circular_number = extract_circular_number(full_text, file.filename)

    # Resolve precedent chain
    from services.precedent_tracker import resolve_precedent_chain
    chain_result = await resolve_precedent_chain(full_text, database)
    precedent_chain = chain_result.get("precedent_chain", [])
    
    # Audit trail: Archive superseded gaps & update status
    import uuid
    superseded_gaps_count = 0
    if is_master_circular:
        for old_ref in supersedes_circulars:
            old_circ = await database.circulars.find_one({
                "$or": [
                    {"circular_number": old_ref},
                    {"circular_id": old_ref}
                ]
            })
            if old_circ:
                old_circ_id = old_circ["circular_id"]
                cursor = database.gap_queue.find({
                    "circular_id": old_circ_id,
                    "triage_status": {"$in": ["assigned", "open"]}
                })
                async for gap in cursor:
                    gap_id = gap["gap_id"]
                    superseded_gaps_count += 1
                    
                    # Update status
                    await database.gap_queue.update_one(
                        {"gap_id": gap_id},
                        {"$set": {"triage_status": "superseded", "classification_reason": f"Superseded by Master Circular {circular_number}"}}
                    )
                    
                    # Store copy in archive for audit
                    gap["triage_status"] = "superseded"
                    gap["classification_reason"] = f"Superseded by Master Circular {circular_number}"
                    await database.gaps_archive.update_one({"gap_id": gap_id}, {"$set": gap}, upsert=True)
                    
                    # Notify assigned employee & HOD
                    if gap.get("assigned_employee"):
                        await database.notifications.insert_one({
                            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                            "user_id": gap["assigned_employee"],
                            "title": "Task Superseded",
                            "message": f"Gap {gap_id} has been marked as superseded by the new Master Circular {circular_number}.",
                            "type": "info",
                            "is_read": False,
                            "created_at": datetime.utcnow()
                        })
                    if gap.get("assigned_hod"):
                        await database.notifications.insert_one({
                            "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                            "user_id": gap["assigned_hod"],
                            "title": "Task Superseded",
                            "message": f"Gap {gap_id} in your department has been marked as superseded by the new Master Circular {circular_number}.",
                            "type": "info",
                            "is_read": False,
                            "created_at": datetime.utcnow()
                        })

    # Save Circular to DB
    doc = {
        "circular_id": circular_id,
        "circular_number": circular_number,
        "title": file.filename,
        "issuer": issuer,
        "date_issued": datetime.utcnow(),
        "ingestion_status": status,
        "clauses_extracted": len(clauses),
        "parser_version": "v4.0_banking_hardened",
        "pages_processed": max(1, max((c.page_number or 1) for c in clauses)) if clauses else 1,
        "processing_time_ms": duration_ms,
        "extraction_confidence": confidence,
        "clauses": [c.model_dump() for c in clauses],
        "full_text": full_text,
        "raw_bytes_hash": preview_hash,
        "gridfs_id": gridfs_id,
        "uploaded_by": current_user["emp_id"],
        # Hardening additions
        "intent": intent,
        "is_master_circular": is_master_circular,
        "supersedes_circulars": supersedes_circulars,
        "applicability_conditions": applicability_conditions,
        "precedent_chain": precedent_chain,
        "structured_blocks": structured_blocks
    }
    
    await database.circulars.insert_one(doc)

    # Auto-run gap detection (only if applicable)
    new_gaps_count = 0
    if status in ("fully_parsed", "partially_parsed"):
        try:
            res_gap = await detect_gaps_for_circular(circular_id, database)
            new_gaps_count = res_gap.confirmed
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Auto-gap detection failed on upload: {e}")

    # Notify all users if it's a Master Circular
    if is_master_circular:
        # Notify all HODs and Admins
        hod_cursor = database.users.find({"role": "dept_head"})
        async for hod in hod_cursor:
            await database.notifications.insert_one({
                "notification_id": f"NOTIF-{str(uuid.uuid4())[:8].upper()}",
                "user_id": hod["emp_id"],
                "title": "Master Circular Uploaded",
                "message": f"Master Circular {circular_number} has been uploaded, superseding older directives. Affected policies re-evaluated.",
                "type": "policy_updated",
                "is_read": False,
                "created_at": datetime.utcnow()
            })

    return {
        "circular_id": circular_id,
        "circular_number": circular_number,
        "ingestion_status": status,
        "clauses_extracted": len(clauses),
        "processing_time_ms": duration_ms,
        "issuer": issuer,
        "intent": intent,
        "is_master_circular": is_master_circular,
        "supersedes_circulars": supersedes_circulars,
        "superseded_gaps_count": superseded_gaps_count,
        "new_gaps_detected": new_gaps_count,
        "status": "created"
    }

@router.get("", response_model=dict)
async def list_circulars(
    current_user: dict = Depends(get_current_user),
):
    database = get_db()
    cursor = database.circulars.find({}).sort("date_issued", -1)
    circulars = []
    async for c in cursor:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("date_issued"), datetime):
            c["date_issued"] = c["date_issued"].isoformat()
        circulars.append(c)

    return {
        "circulars": circulars,
        "stats": {
            "total": len(circulars),
            "fully_parsed": sum(1 for c in circulars if c.get("ingestion_status") == "fully_parsed"),
            "failed": sum(1 for c in circulars if c.get("ingestion_status") == "failed")
        }
    }

@router.get("/by-id", response_model=CircularResponse)
async def get_circular_by_query(circular_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    doc = await database.circulars.find_one({"circular_id": circular_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Circular not found")
    return CircularResponse(**doc)

@router.get("/{circular_id:path}", response_model=CircularResponse)
async def get_circular(circular_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    doc = await database.circulars.find_one({"circular_id": circular_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Circular not found")
    return CircularResponse(**doc)

@router.get("/{circular_id:path}/download")
async def download_circular(circular_id: str, current_user: dict = Depends(get_current_user)):
    database = get_db()
    doc = await database.circulars.find_one({"circular_id": circular_id})
    if not doc or not doc.get("gridfs_id"):
        raise HTTPException(status_code=404, detail="File not found")

    content, filename, content_type = await download_file_from_gridfs(doc["gridfs_id"])

    return Response(
        content=content,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@router.delete("/{circular_id:path}")
async def delete_circular(circular_id: str, current_user: dict = Depends(get_current_user)):
    if current_user.get("role") not in INGESTION_ROLES:
        raise HTTPException(403, "Circular deletion access required")
        
    database = get_db()
    
    # 1. Fetch circular record to get gridfs_id
    doc = await database.circulars.find_one({"circular_id": circular_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Circular not found")
        
    # 2. Delete file from GridFS
    gridfs_id = doc.get("gridfs_id")
    if gridfs_id:
        try:
            await delete_file_from_gridfs(gridfs_id)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to delete circular file from GridFS: {e}")
            
    # 3. Delete from gaps queue and archive
    await database.gap_queue.delete_many({"circular_id": circular_id})
    await database.gaps_archive.delete_many({"circular_id": circular_id})
    
    # 4. Delete circular record from DB
    await database.circulars.delete_one({"circular_id": circular_id})
    
    # 5. Log deletion to audit ledger
    from services.audit_logger import append_audit_log
    await append_audit_log(
        database=database,
        action="delete_circular",
        actor_id=current_user["emp_id"],
        details=f"Deleted circular {circular_id} ({doc.get('circular_number', 'Unknown')}) and associated gaps",
        target_id=circular_id
    )
    
    return {"status": "success", "message": f"Circular {circular_id} deleted successfully"}
