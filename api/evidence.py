from fastapi import APIRouter, HTTPException, Depends

from database import db
from services.validator_service import validate_evidence
from api.auth import get_current_user

router = APIRouter(prefix="/api/evidence", tags=["Evidence"])
EVIDENCE_ROLES = {"super_admin", "admin", "compliance_officer", "department_head", "auditor"}


def get_db():
    return db.client.suraksha_maps


@router.post("/{evidence_id}/validate")
async def validate_evidence_endpoint(
    evidence_id: str,
    current_user: dict = Depends(get_current_user),
):
    if current_user.get("role") not in EVIDENCE_ROLES:
        raise HTTPException(403, "Evidence validation access required")
    database = get_db()
    try:
        return await validate_evidence(database, evidence_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Validation failed: {e}")
