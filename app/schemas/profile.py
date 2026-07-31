from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from app.models.enums import RemotePreference, SeniorityLevel
from app.schemas.common import ORMModel


class ProfileWrite(BaseModel):
    """Create/update payload.

    ``email`` identifies the owning user; a profile cannot exist without one,
    and the endpoint creates the user on first write so a single-user install
    needs no separate registration step.
    """

    email: EmailStr
    full_name: str | None = Field(default=None, max_length=255)

    preferred_roles: list[str] = Field(default_factory=list, max_length=50)
    preferred_locations: list[str] = Field(default_factory=list, max_length=50)
    preferred_skills: list[str] = Field(default_factory=list, max_length=200)
    industries: list[str] = Field(default_factory=list, max_length=50)
    excluded_keywords: list[str] = Field(default_factory=list, max_length=100)

    seniority: SeniorityLevel = SeniorityLevel.UNKNOWN
    remote_preference: RemotePreference = RemotePreference.ANY
    years_experience: int | None = Field(default=None, ge=0, le=60)
    requires_visa_sponsorship: bool = False

    desired_salary_min: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=8)

    match_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    include_unknown_location: bool = True


class ProfileUpdate(BaseModel):
    """Partial update. Every field optional; unset fields are left alone."""

    full_name: str | None = None
    preferred_roles: list[str] | None = None
    preferred_locations: list[str] | None = None
    preferred_skills: list[str] | None = None
    industries: list[str] | None = None
    excluded_keywords: list[str] | None = None
    seniority: SeniorityLevel | None = None
    remote_preference: RemotePreference | None = None
    years_experience: int | None = Field(default=None, ge=0, le=60)
    requires_visa_sponsorship: bool | None = None
    desired_salary_min: int | None = Field(default=None, ge=0)
    salary_currency: str | None = None
    match_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    include_unknown_location: bool | None = None


class ProfileRead(ORMModel):
    id: uuid.UUID
    user_id: uuid.UUID
    preferred_roles: list[str]
    preferred_locations: list[str]
    preferred_skills: list[str]
    industries: list[str]
    excluded_keywords: list[str]
    seniority: SeniorityLevel
    remote_preference: RemotePreference
    years_experience: int | None
    requires_visa_sponsorship: bool
    desired_salary_min: int | None
    salary_currency: str | None
    match_threshold: float
    include_unknown_location: bool
    created_at: datetime
    updated_at: datetime
