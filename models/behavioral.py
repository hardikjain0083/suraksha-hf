from pydantic import BaseModel, Field
from typing import List, Optional, Literal

class KeystrokeData(BaseModel):
    dwell_times: List[float] = Field(default_factory=list, description="List of hold durations in ms")
    flight_times: List[float] = Field(default_factory=list, description="List of time between keys in ms")
    typing_speed: float = Field(default=0.0, description="Words per minute")
    error_rate: float = Field(default=0.0, description="0.0 to 1.0")
    total_keys: int = Field(default=0)

class MouseClick(BaseModel):
    x: float
    y: float
    timestamp: float
    type: Literal['left', 'right']

class MouseTrajectoryPoint(BaseModel):
    x: float
    y: float
    timestamp: float

class MouseData(BaseModel):
    velocities: List[float] = Field(default_factory=list)
    click_patterns: List[MouseClick] = Field(default_factory=list)
    idle_times: List[float] = Field(default_factory=list)
    trajectory: List[MouseTrajectoryPoint] = Field(default_factory=list)

class SessionContext(BaseModel):
    time_of_day: float = Field(default=0.0)
    day_of_week: int = Field(default=0)
    device_fingerprint_hash: str = Field(default="")

class BehavioralPayload(BaseModel):
    keystroke: Optional[KeystrokeData] = None
    mouse: Optional[MouseData] = None
    session_context: Optional[SessionContext] = None

class BehavioralBaseline(BaseModel):
    status: Literal['pending', 'active', 'failed'] = 'pending'
    rounds_completed: int = 0
    raw_data: List[dict] = Field(default_factory=list)
