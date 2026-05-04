from __future__ import annotations

from .config import PRConfig
from .models import DiffSummary, Issue
from .privacy import sanitize_user_paths


def render_template(template: str, issue: Issue, **values: str) -> str:
    data = {"number": str(issue.number), "title": issue.title, **values}
    return template.format(**data)


def changed_files_text(diff: DiffSummary) -> str:
    return "\n".join(f"- {path}" for path in diff.changed_files) or "No changed files."


def build_pr_body(config: PRConfig, issue: Issue, verification_summary: str, diff: DiffSummary, agent_notes: str = "") -> str:
    return sanitize_user_paths(
        render_template(
            config.body_template,
            issue,
            verification_summary=verification_summary,
            changed_files=changed_files_text(diff),
            agent_notes=agent_notes or "See diff for implementation details.",
        )
    )
