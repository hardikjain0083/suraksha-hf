from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


MapStatus = Literal[
    "pending_head_review",
    "approved",
    "assigned",
    "in_progress",
    "under_validation",
    "resolved",
    "rejected",
    "escalated",
    "pending_admin_assignment",
    "draft",
    "open",
    "complete",
    "cancelled",
]
RiskLevel = Literal["critical", "high", "medium", "low"]
ProvenanceType = Literal["circular", "clause", "gap", "policy", "department", "employee"]


class ProvenanceNode(BaseModel):
    type: ProvenanceType
    id: str
    title: Optional[str] = None
    text: Optional[str] = None
    classification: Optional[str] = None
    name: Optional[str] = None


class EvidenceItem(BaseModel):
    evidence_type: str
    label: str
    required: bool = True
    uploaded: bool = False
    evidence_id: Optional[str] = None
    validation_status: Optional[str] = None
    confidence: Optional[float] = None
    file_url: Optional[str] = None
    uploaded_at: Optional[datetime] = None


class AuditTrailEntry(BaseModel):
    timestamp: datetime
    user_id: str
    user_name: Optional[str] = None
    action: str
    details: Optional[str] = None


class TimelineEntry(BaseModel):
    stage: str
    label: str
    timestamp: Optional[datetime] = None
    completed: bool = False


class SimilarMapRef(BaseModel):
    map_id: str
    title: str
    status: str
    resolved_at: Optional[datetime] = None


class MapGenerateRequest(BaseModel):
    gap_id: str
    officer_id: Optional[str] = "system"
    officer_name: Optional[str] = "Compliance Officer"


class MapGenerateResponse(BaseModel):
    map_id: str
    status: MapStatus
    provenance_path: list[ProvenanceNode]
    priority_score: int
    title: str
    deadline: datetime
    is_historical_match: bool


class MapApproveBody(BaseModel):
    officer_id: str = "emp_0"
    officer_name: str = "Compliance Officer"
    department_id: Optional[str] = None
    evidence_types: Optional[list[str]] = None
    deadline: Optional[datetime] = None
    risk_level: Optional[RiskLevel] = None
    title: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class MapRejectBody(BaseModel):
    officer_id: str = "emp_0"
    officer_name: str = "Compliance Officer"
    reason: str = "Not applicable"


class MapExtendBody(BaseModel):
    new_deadline: datetime
    reason: str
    approver_id: str = "emp_0"
    approver_name: str = "Department Head"


class MapEscalateBody(BaseModel):
    reason: str
    officer_id: str = "emp_0"
    officer_name: str = "Compliance Officer"
    new_deadline_days: int = 14


class MapBulkApproveBody(BaseModel):
    map_ids: list[str]
    officer_id: str = "emp_0"
    officer_name: str = "Compliance Officer"


class MapListItem(BaseModel):
    map_id: str
    title: str
    status: MapStatus
    owner_department_id: str
    department_name: Optional[str] = None
    assigned_to: Optional[str] = None
    assignee_name: Optional[str] = None
    deadline: datetime
    priority_score: int
    risk_level: RiskLevel
    circular_id: str
    gap_id: Optional[str] = None
    evidence_completion_pct: float = 0.0
    days_until_deadline: int = 0
    is_overdue: bool = False
    created_at: Optional[datetime] = None


class MapListResponse(BaseModel):
    items: list[MapListItem]
    total: int
    page: int
    page_size: int


class MapDetailResponse(BaseModel):
    map_id: str
    title: str
    description: str
    requirements: list[str] = Field(default_factory=list)
    status: MapStatus
    priority_score: int
    risk_level: RiskLevel
    circular_id: str
    policy_id: Optional[str] = None
    gap_id: Optional[str] = None
    owner_department_id: str
    department_name: Optional[str] = None
    assigned_to: Optional[str] = None
    assignee_name: Optional[str] = None
    deadline: datetime
    provenance_path: list[ProvenanceNode]
    evidence_items: list[EvidenceItem]
    audit_trail: list[AuditTrailEntry]
    timeline: list[TimelineEntry]
    similar_past_maps: list[SimilarMapRef] = Field(default_factory=list)
    historical_match_count: int = 0
    confidence_score: float = 0.0
    is_historical_match: bool = False
    clause_text: Optional[str] = None
    escalation_status: Optional[str] = None
    parent_map_id: Optional[str] = None
    created_at: datetime
    approved_at: Optional[datetime] = None
    issuer: Optional[str] = None


class TriageMapCard(BaseModel):
    map_id: Optional[str] = None
    gap_id: str
    title: str
    description: str
    status: str
    historical_match_count: int
    confidence_score: float
    similar_policies_count: int = 0
    provenance_path: list[ProvenanceNode]
    similar_past_maps: list[SimilarMapRef] = Field(default_factory=list)
    suggested_evidence: list[str] = Field(default_factory=list)
    deadline: datetime
    priority_score: int
    risk_level: RiskLevel
    clause_text: str
    circular_id: str
    circular_title: Optional[str] = None
    routing: str
    gap_status: str
    severity: Optional[str] = None
    department_id: Optional[str] = None
    department_name: Optional[str] = None
    suggested_department_id: Optional[str] = None
    suggested_map_title: Optional[str] = None


class TriageAction(BaseModel):
    action_id: str
    map_id: Optional[str] = None
    gap_id: Optional[str] = None
    timestamp: datetime
    officer_name: str
    decision: str
    title: Optional[str] = None


class TriageDashboardResponse(BaseModel):
    stats: dict[str, Any]
    auto_routed: list[TriageMapCard]
    pending_review: list[TriageMapCard]
    recently_processed: list[TriageAction]
