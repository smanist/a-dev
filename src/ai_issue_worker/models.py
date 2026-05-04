from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    state: str
    url: str | None = None
    updated_at: str | None = None
    id: int | None = None

    @classmethod
    def from_gh(cls, data: dict[str, Any]) -> "Issue":
        labels = data.get("labels") or []
        names = [
            item["name"] if isinstance(item, dict) else str(item) for item in labels
        ]
        raw_id = data.get("id") or data.get("databaseId") or data.get("database_id")
        issue_id = (
            int(raw_id)
            if isinstance(raw_id, int | str) and str(raw_id).isdigit()
            else None
        )
        return cls(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            labels=names,
            state=data.get("state") or "",
            url=data.get("url"),
            updated_at=data.get("updatedAt") or data.get("updated_at"),
            id=issue_id,
        )


@dataclass(frozen=True)
class CreatedIssue:
    number: int
    title: str
    url: str
    id: int


@dataclass
class DiscussionComment:
    source: str
    body: str
    author: str | None = None
    created_at: str | None = None
    url: str | None = None

    @classmethod
    def from_gh(cls, data: dict[str, Any], source: str) -> "DiscussionComment":
        user = data.get("user") or {}
        created_at = data.get("created_at") or data.get("submitted_at")
        url = data.get("html_url") or data.get("url")
        return cls(
            source=source,
            body=data.get("body") or "",
            author=user.get("login") if isinstance(user, dict) else None,
            created_at=created_at,
            url=url,
        )


@dataclass
class JobRecord:
    issue_number: int
    issue_title: str
    branch_name: str
    worktree_path: str
    status: str
    started_at: str
    base_branch: str | None = None
    stack_depth: int = 0
    blocker_issue_numbers: list[int] = field(default_factory=list)
    finished_at: str | None = None
    pr_url: str | None = None
    error_summary: str | None = None
    changed_files: list[str] = field(default_factory=list)
    verifier_passed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobRecord":
        return cls(**data)


@dataclass
class AgentResult:
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool


@dataclass
class CommandResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float


@dataclass
class VerifyResult:
    passed: bool
    commands: list[CommandResult]


@dataclass
class DiffSummary:
    changed_files: list[str]
    diff_stat: str
    diff_line_count: int
    rejected: bool
    rejection_reason: str | None
