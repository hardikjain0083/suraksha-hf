import os
import secrets
import warnings
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Union
import json


class Settings(BaseSettings):
    mongodb_uri: str = "mongodb://localhost:27017"
    jwt_secret: str = secrets.token_urlsafe(32)   # auto-generated if not set
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440        # 24 hours
    backend_cors_origins: Union[str, List[str]] = [
        "http://localhost:5173",
        "https://your-frontend.vercel.app",
    ]
    environment: str = "development"
    embedding_model: str = "all-MiniLM-L6-v2"
    demo_mode: bool = False  # Set DEMO_MODE=true in .env for hackathon judge demos only
    gemini_api_key: str | None = None

    # OCR Configurations
    ocr_engine: str = "auto"
    ocr_cloud_provider: str = "aws"
    
    aws_textract_region: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    
    ocr_tesseract_min_confidence: float = 0.60
    ocr_cloud_min_confidence: float = 0.75
    
    ocr_max_pages: int = 200
    ocr_large_doc_sample_rate: int = 5
    
    ocr_dpi: int = 300
    ocr_parallel_workers: int = 4
    
    ocr_retry_attempts: int = 3
    ocr_retry_backoff: int = 2

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    def get_cors_origins(self) -> List[str]:
        if isinstance(self.backend_cors_origins, str):
            try:
                return json.loads(self.backend_cors_origins)
            except json.JSONDecodeError:
                return [o.strip() for o in self.backend_cors_origins.split(",")]
        return self.backend_cors_origins

    def __init__(self, **data):
        super().__init__(**data)
        # Warn if default/weak JWT secret is used
        weak_secrets = {"your-jwt-secret", "secret", "123456", "changeme"}
        if self.jwt_secret in weak_secrets:
            warnings.warn(
                "⚠️  SECURITY: JWT_SECRET is weak. Set a strong random string in .env",
                stacklevel=2,
            )


settings = Settings()

BANK_PROFILE = {
    "entity_type": "scheduled_commercial_bank",  # e.g., 'scheduled_commercial_bank', 'rrb', 'ucb', 'sfb', 'nbfc'
    "asset_size_inr_crore": 15000,
    "is_d_sib": False,
    "has_ad_category1_license": True,
}

CIRCULAR_PREFIX_MAP = {
    "DBOD": {"dept_hint": ["Operations", "Compliance"], "category": "Banking Operations"},
    "DOR.STR.REC": {"dept_hint": ["Risk Management", "Compliance"], "category": "Prudential Regulation"},
    "DOR.STR": {"dept_hint": ["Risk Management", "Compliance"], "category": "Prudential Regulation"},
    "DOR.ACC": {"dept_hint": ["Finance"], "category": "Accounting"},
    "DoR.FIN": {"dept_hint": ["Finance"], "category": "Financial Regulation"},
    "DPSS": {"dept_hint": ["IT Security", "Operations"], "category": "Payments"},
    "FEMA": {"dept_hint": ["Treasury", "Operations"], "category": "Foreign Exchange"},
}

FINANCIAL_STOPWORDS = {
    "bank", "circular", "rbi", "reserve", "of", "india",
    "section", "subsection", "annexure", "schedule", "hereinafter",
    "thereof", "pursuant", "notwithstanding", "aforesaid", "herein",
    "whereas", "whereby", "aforementioned", "therein",
    "above", "below", "hereby", "thereby", "furthermore", "moreover"
}

DIRECTIVE_KEYWORDS = {
    "shall", "must", "should", "are required to", "banks are directed to",
    "it is mandatory", "with immediate effect", "compliance required by",
    "shall ensure", "shall maintain", "shall submit", "shall furnish",
    "shall comply", "shall adhere", "shall implement", "shall establish",
    "shall review", "shall formulate", "shall put in place",
    "are advised to", "are encouraged to", "may consider",
    "shall develop", "shall monitor", "shall report", "shall conduct",
    "shall appoint", "shall participate", "shall ensure"
}

MATCH_THRESHOLD = 0.75
PARTIAL_THRESHOLD = 0.30

SIGNAL_WEIGHTS = {
    "tfidf": 0.30,
    "jaccard": 0.20,
    "fuzzy": 0.15,
    "embedding": 0.00,
    "frame": 0.35
}

DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
