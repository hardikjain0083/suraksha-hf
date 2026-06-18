from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
from datetime import datetime

class KeywordComplianceResult(BaseModel):
    exact_found: List[str] = []
    policy_has: List[str] = []
    policy_missing: List[str] = []
    fuzzy_matched: bool = False
    severity: str = "medium"
    passed: bool = False

class PolicyMatch(BaseModel):
    policy_id: str
    title: str
    department: str
    similarity: float
    keyword_compliance: Dict[str, KeywordComplianceResult] = {}
    overall_pass: bool = False
    explanation: str = ""
    full_text: Optional[str] = None

class JudgeExplanationStep(BaseModel):
    stage: str
    title: str
    technical_detail: str
    business_impact: str
    result: str  # "pass" | "fail" | "review"
    data: Dict[str, Any] = {}

class GapCheckResult(BaseModel):
    clause_number: Optional[str] = None
    clause_text: str
    obligation_type: Optional[str] = None
    severity: Optional[str] = None
    gap_status: str = "pending"  # covered | suspected | confirmed | data_error
    top_policy_matches: List[PolicyMatch] = []
    similarity_score: Optional[float] = None
    classification_reason: str = ""
    judge_explanation: List[JudgeExplanationStep] = []
    historical_match_count: int = 0
    routing: str = "pending_review"  # auto_routed | pending_review
    
    similarity_scores: Dict[str, float] = {}
    rbi_action: Optional[str] = None
    rbi_modality: Optional[str] = None
    policy_action: Optional[str] = None
    policy_modality: Optional[str] = None
    mismatch_description: Optional[str] = None

class GapDetectionResponse(BaseModel):
    circular_id: str
    total_clauses_analyzed: int = 0
    covered: int = 0
    suspected: int = 0
    confirmed: int = 0
    data_errors: int = 0
    coverage_rate: float = 0.0
    gaps: List[GapCheckResult] = []
    detection_time_ms: int = 0
    status: str = "completed"  # completed | blocked
    reason: Optional[str] = None

class GapQueueEntry(BaseModel):
    gap_id: str
    circular_id: str
    circular_title: Optional[str] = None
    clause_number: Optional[str] = None
    clause_text: str
    obligation_type: Optional[str] = None
    severity: Optional[str] = None
    gap_status: str
    similarity_score: Optional[float] = None
    top_policy_id: Optional[str] = None
    top_policy_title: Optional[str] = None
    classification_reason: str
    routing: str
    triage_status: str = "assigned"  # assigned | resolved | cancelled | superseded
    created_at: datetime = Field(default_factory=datetime.utcnow)
    judge_explanation: List[JudgeExplanationStep] = []
    historical_match_count: int = 0
    page_number: Optional[int] = 1
    matched_policy_line_num: Optional[int] = None
    department_id: Optional[str] = None
    assigned_hod: Optional[str] = None
    assigned_employee: Optional[str] = None
    due_date: Optional[datetime] = None
    fixed_policy_content: Optional[str] = None
    remaining_gaps: Optional[List[str]] = []
    is_fixed: bool = False
    
    # Hardening additions
    guideline_substance_hash: Optional[str] = None
    supersedes_gap_id: Optional[str] = None
    parent_guideline_id: Optional[str] = None
    source: str = "circular_upload"  # circular_upload, fix_regression, manual
    is_ambiguous: bool = False
    ambiguous_departments: List[str] = []
    
    similarity_scores: Dict[str, float] = {}
    rbi_action: Optional[str] = None
    rbi_modality: Optional[str] = None
    policy_action: Optional[str] = None
    policy_modality: Optional[str] = None
    mismatch_description: Optional[str] = None
