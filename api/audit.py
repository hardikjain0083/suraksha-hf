from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Any
import logging

from database import db
from services.audit_logger import verify_audit_chain

router = APIRouter()
logger = logging.getLogger(__name__)

@router.get("/")
async def get_audit_logs(limit: int = 100, skip: int = 0):
    try:
        database = db.client.suraksha_maps
        cursor = database.audit_logs.find({}).sort([("timestamp", -1)]).skip(skip).limit(limit)
        logs = await cursor.to_list(length=limit)
        
        # Convert ObjectId to string for JSON serialization
        for log in logs:
            log["_id"] = str(log["_id"])
        return logs
    except Exception as e:
        logger.error(f"Failed to fetch audit logs: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch audit logs")

@router.get("/verify")
async def verify_chain():
    try:
        database = db.client.suraksha_maps
        result = await verify_audit_chain(database)
        return result
    except Exception as e:
        logger.error(f"Failed to verify audit chain: {e}")
        raise HTTPException(status_code=500, detail="Failed to verify audit chain")
