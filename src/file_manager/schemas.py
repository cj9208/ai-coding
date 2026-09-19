from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TeamOut(ORMModel):
    id: int
    name: str
    member_count: int = 0


class MemberCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    team_id: int | None = None


class MemberUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    team_id: int | None = None


class MemberOut(ORMModel):
    id: int
    name: str
    team_id: int | None = None
    team_name: str | None = None


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""


class ProjectOut(ORMModel):
    id: int
    name: str
    description: str
    file_count: int = 0


class FileOut(ORMModel):
    id: int
    original_filename: str
    extension: str | None
    size: int
    title: str | None
    notes: str
    tags: str
    project_id: int
    project_name: str
    uploader_id: int | None
    uploader_name: str
    team_id: int | None
    team_name: str | None
    created_at: datetime | None
