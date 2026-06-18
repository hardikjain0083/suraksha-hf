from pydantic import BaseModel, Field
from typing import List, Optional

class Asset(BaseModel):
    asset_id: str
    name: str
    category: str  # e.g., "sensitive_customer_data", "perimeter_security", "transaction_processing"
    sensitivity: str  # e.g., "critical", "high", "medium", "low"
    software_stack: List[str]  # e.g., ["Apache Tomcat 9.0.41", "OpenJDK 11", "Oracle DB 19c"]
    owner_department_id: str
    ip_address: Optional[str] = None

    class Config:
        json_schema_extra = {
            "example": {
                "asset_id": "SYS-CORE-001",
                "name": "Core Banking System",
                "category": "sensitive_customer_data",
                "sensitivity": "critical",
                "software_stack": ["Oracle DB 19c", "WebLogic Server 14.1.1", "RedHat Enterprise Linux 8.4"],
                "owner_department_id": "DEPT-IT",
                "ip_address": "10.0.1.20"
            }
        }
