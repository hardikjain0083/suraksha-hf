from pydantic import BaseModel, EmailStr, Field
from typing import Optional, Dict, Any
from .behavioral import BehavioralPayload

class UserRegister(BaseModel):
    full_name: str = Field(..., min_length=2)
    email: EmailStr
    mobile: str
    department: str
    designation: Optional[str] = None
    password: str = Field(..., min_length=8)

class UserLogin(BaseModel):
    emp_id: str
    password: str
    behavioral_data: Optional[BehavioralPayload] = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    session_id: str
    risk_score: int
    access_level: str
    behavioral_breakdown: Dict[str, Any]
    requires_hardware_token: bool
    user: Dict[str, Any]

class EnrollmentResponse(BaseModel):
    round: int
    quality_score: float
    status: str
    round_scores: Dict[str, float]
    models_trained: bool
    message: str

class VerificationRequest(BaseModel):
    behavioral_data: BehavioralPayload

class VerificationResponse(BaseModel):
    current_risk_score: int
    anomaly_detected: bool

class CriticalActionRequest(BaseModel):
    action: str
    phrase_typed: str
    behavioral_data: BehavioralPayload

class CriticalActionResponse(BaseModel):
    allowed: bool
    score: int
    required_action: Optional[str] = None
