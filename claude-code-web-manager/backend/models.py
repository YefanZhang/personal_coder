from enum import Enum
from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskMode(str, Enum):
    EXECUTE = "execute"
    PLAN = "plan"


class TaskPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class Task(BaseModel):
    id: int
    title: str
    prompt: str
    status: TaskStatus = TaskStatus.PENDING
    mode: TaskMode = TaskMode.EXECUTE
    priority: TaskPriority = TaskPriority.MEDIUM

    worktree_branch: Optional[str] = None
    working_directory: Optional[str] = None
    worker_pid: Optional[int] = None

    output: Optional[str] = None
    plan: Optional[str] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cost_usd: Optional[float] = None

    depends_on: list[int] = []

    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    repo_path: Optional[str] = None
    tags: list[str] = []


class TaskLog(BaseModel):
    id: int
    task_id: int
    timestamp: datetime
    level: str
    message: str
    raw_output: Optional[str] = None


class CreateTaskRequest(BaseModel):
    title: str
    prompt: str
    mode: TaskMode = TaskMode.EXECUTE
    priority: TaskPriority = TaskPriority.MEDIUM
    depends_on: list[int] = []
    repo_path: Optional[str] = None
    tags: list[str] = []
