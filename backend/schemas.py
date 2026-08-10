"""Request/response models."""
from datetime import datetime
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field


class ScheduleRequest(BaseModel):
    title: str = Field(default="Meeting", max_length=255)
    description: Optional[str] = None
    attendees: list[str] = Field(default_factory=list)  # names or emails
    duration_minutes: int = Field(default=30, ge=5, le=480)
    location_type: Literal["virtual", "in-person"] = "virtual"
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None


class SmartScheduleRequest(BaseModel):
    attendees: list[str] = Field(default_factory=list)  # explicit chips (authoritative)
    text: str = Field(default="", max_length=2000)       # optional NL for time/title/duration
    duration_minutes: Optional[int] = Field(default=None, ge=5, le=480)
    location_type: Optional[Literal["virtual", "in-person"]] = None


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    attendees: list[str] = Field(default_factory=list)   # chips (authoritative)
    context: Optional[dict[str, Any]] = None             # conversational scheduling state


class BookRequest(BaseModel):
    title: str = Field(default="Meeting", max_length=255)
    description: Optional[str] = None
    location: Optional[str] = None
    location_type: Literal["virtual", "in-person"] = "virtual"
    attendees: list[str] = Field(default_factory=list)  # resolved emails
    start: datetime
    end: datetime
