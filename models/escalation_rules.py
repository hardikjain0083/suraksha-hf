from pydantic import BaseModel, Field
from typing import List, Optional

class EscalationRule(BaseModel):
    rule_id: str
    name: str
    condition_severity: str = "critical"  # e.g., "critical", "high"
    condition_system_sensitivity: str = "critical"  # e.g., "critical", "high"
    escalate_to: str = "CISO"  # e.g., "CISO", "compliance_officer"
    notification_channel: str = "email"
    active: bool = True

    class Config:
        json_schema_extra = {
            "example": {
                "rule_id": "rule_ciso_001",
                "name": "Critical CVE on Sensitive Customer Data System",
                "condition_severity": "critical",
                "condition_system_sensitivity": "critical",
                "escalate_to": "CISO",
                "notification_channel": "email",
                "active": True
            }
        }
