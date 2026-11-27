from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class ExplainMode(str, Enum):
    LINE_BY_LINE = "line_by_line"
    WIKI = "wiki"


class Granularity(str, Enum):
    LITERAL = "literal"
    BLOCKS = "blocks"


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLONING = "cloning"
    INDEXING = "indexing"
    EXPLAINING = "explaining"
    CORRELATING = "correlating"
    REVISING = "revising"
    ASSEMBLING = "assembling"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


class ExplainRequest(BaseModel):
    repo_url: str = Field(..., min_length=1)
    model: str | None = None
    mode: ExplainMode = ExplainMode.WIKI
    granularity: Granularity = Granularity.BLOCKS
    refine: bool = True
    max_workers: int = Field(default=4, ge=1, le=16)
    max_files: int = Field(default=150, ge=1, le=500)
    focus_paths: list[str] | None = None
    live: bool = False


class JobProgress(BaseModel):
    done: int = 0
    total: int = 0
    workers: int = 0


class RevisionRecord(BaseModel):
    anchor: str
    file: str
    start_line: int
    end_line: int
    instruction: str
    before: str
    after: str


class JobState(BaseModel):
    job_id: str
    repo_url: str
    model: str | None = None
    mode: ExplainMode
    granularity: Granularity
    status: JobStatus = JobStatus.QUEUED
    progress: JobProgress = Field(default_factory=JobProgress)
    partial: bool = False
    files: list[str] = Field(default_factory=list)
    error: str | None = None
    text: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    cancel_requested: bool = False
    live_available: bool = False
    revisions: list[RevisionRecord] = Field(default_factory=list)
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
