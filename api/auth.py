from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from motor.motor_asyncio import AsyncIOMotorDatabase
import uuid
import re

from database import db
from config import settings
from core.security import get_password_hash, verify_password, create_access_token
from models.auth import (
    UserRegister, UserLogin, TokenResponse,
    EnrollmentResponse, VerificationRequest, VerificationResponse,
    CriticalActionRequest, CriticalActionResponse,
)

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

# ─────────────────────────────────────────────────────────────
#  Dependency helpers
# ─────────────────────────────────────────────────────────────

def get_db() -> AsyncIOMotorDatabase:
    return db.client.suraksha_maps


async def get_current_user(
    token: str = Depends(oauth2_scheme),
) -> dict:
    """Decode JWT and return the user document from MongoDB."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
        emp_id: str | None = payload.get("sub")
        if emp_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    database = get_db()
    user = await database.users.find_one({"emp_id": emp_id})
    if user is None:
        raise credentials_exception
    return user


# ─────────────────────────────────────────────────────────────
#  Password strength validation
# ─────────────────────────────────────────────────────────────

def _validate_password(password: str) -> None:
    """Enforce: 8+ chars, 1 uppercase, 1 digit, 1 special character."""
    errors = []
    if len(password) < 8:
        errors.append("at least 8 characters")
    if not re.search(r"[A-Z]", password):
        errors.append("at least 1 uppercase letter")
    if not re.search(r"\d", password):
        errors.append("at least 1 number")
    if not re.search(r"[^A-Za-z0-9]", password):
        errors.append("at least 1 special character (e.g. @#$!)")
    if errors:
        raise HTTPException(
            status_code=400,
            detail=f"Password must contain: {', '.join(errors)}",
        )


# ─────────────────────────────────────────────────────────────
#  Endpoints
# ─────────────────────────────────────────────────────────────

@router.post("/register", response_model=dict, status_code=201)
async def register_user(user_data: UserRegister):
    """Register a new employee. Returns emp_id and access_token for enrollment."""
    database = get_db()

    # Duplicate check
    if await database.users.find_one({"email": user_data.email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    # Password strength
    _validate_password(user_data.password)

    # Standardize department ID prefix
    dept_id = user_data.department if user_data.department.startswith("DEPT-") else f"DEPT-{user_data.department.upper()}"
    dept_prefix = dept_id.replace("DEPT-", "")
    
    # Generate sequential employee ID based on department count
    count = await database.users.count_documents({"department_id": dept_id})
    emp_id = f"EMP-{dept_prefix}-{(count + 1):03d}"

    new_user = {
        "emp_id": emp_id,
        "name": user_data.full_name,
        "email": user_data.email,
        "mobile": user_data.mobile,
        "department_id": dept_id,
        "designation": user_data.designation,
        "hashed_password": get_password_hash(user_data.password),
        "role": "compliance_officer",
        "status": "active",
        "behavioral_baseline": {
            "status": "pending",
            "rounds_completed": 0,
            "raw_data": [],
        },
    }

    await database.users.insert_one(new_user)
    access_token = create_access_token(data={"sub": emp_id, "role": "compliance_officer"})

    return {
        "emp_id": emp_id,
        "access_token": access_token,
        "token_type": "bearer",
        "message": "Registration successful. Proceed to enrollment.",
    }


@router.post("/login", response_model=TokenResponse)
async def login(login_data: UserLogin):
    """Authenticate with employee ID and password."""
    database = get_db()

    user = await database.users.find_one({"emp_id": login_data.emp_id})
    if not user or not verify_password(login_data.password, user["hashed_password"]):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
        )

    access_token = create_access_token(
        data={"sub": user["emp_id"], "role": user.get("role", "user")}
    )
    session_id = f"sess_{uuid.uuid4().hex}"

    return TokenResponse(
        access_token=access_token,
        session_id=session_id,
        risk_score=0,
        access_level="green",
        behavioral_breakdown={},
        requires_hardware_token=False,
        user={
            "emp_id": user["emp_id"],
            "full_name": user.get("name", user.get("full_name", "")),
            "department": user.get("department_id", ""),
            "role": user.get("role", "compliance_officer"),
        },
    )


@router.post("/enrollment/round/{round_number}", response_model=EnrollmentResponse)
async def enrollment_round(
    round_number: int,
    current_user: dict = Depends(get_current_user),
):
    return EnrollmentResponse(
        round=round_number,
        quality_score=1.0,
        status="active",
        round_scores={},
        models_trained=True,
        message="Enrollment active."
    )


@router.post("/verify-session", response_model=VerificationResponse)
async def verify_session(
    current_user: dict = Depends(get_current_user),
):
    return VerificationResponse(current_risk_score=0, anomaly_detected=False)


@router.post("/critical-action", response_model=CriticalActionResponse)
async def critical_action(
    current_user: dict = Depends(get_current_user),
):
    return CriticalActionResponse(allowed=True, score=0)


# ─────────────────────────────────────────────────────────────
#  Admin User Management Endpoints
# ─────────────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/admin")

@admin_router.get("/users")
async def list_users(role: str = None, department: str = None):
    database = get_db()
    query = {}
    if role:
        query["role"] = role
    if department:
        query["department_id"] = department
    cursor = database.users.find(query)
    users = []
    async for u in cursor:
        users.append({
            "emp_id": u.get("emp_id"),
            "name": u.get("name", u.get("full_name", "")),
            "email": u.get("email", ""),
            "dept": u.get("department_id", ""),
            "role": u.get("role", "employee"),
            "designation": u.get("designation", u.get("role", "employee").replace("_", " ").title()),
            "baseline_status": "Active" if u.get("behavioral_baseline", {}).get("status") == "active" else "Pending",
            "accessibility_flag": u.get("accessibility_flag", False),
            "assigned_maps": u.get("active_gap_count", 0),
            "status": u.get("status", "active"),
            "availability_status": u.get("availability_status", "available"),
            "max_concurrent_gaps": u.get("max_concurrent_gaps", 5)
        })
    return users

@admin_router.patch("/users/{emp_id}/role")
async def update_user_role(emp_id: str, body: dict):
    database = get_db()
    role = body.get("role")
    res = await database.users.update_one({"emp_id": emp_id}, {"$set": {"role": role}})
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "success", "message": "Role updated"}

@admin_router.post("/users/{emp_id}/reset-enrollment")
async def reset_user_enrollment(emp_id: str):
    database = get_db()
    res = await database.users.update_one(
        {"emp_id": emp_id},
        {"$set": {
            "behavioral_baseline.status": "pending",
            "behavioral_baseline.rounds_completed": 0,
            "behavioral_baseline.raw_data": []
        }}
    )
    if res.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "success", "message": "Enrollment reset"}


