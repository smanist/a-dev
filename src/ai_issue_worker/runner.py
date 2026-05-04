from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .codex_backend import CodexBackend
from .config import ConfigError, IssueSelectionConfig, WorkerConfig, load_config
from .diff_policy import inspect_diff
from .github_gh import GHClient, GHError
from .issue_selection import candidate_issues
from .jobs import (
    append_artifact_log,
    copy_artifact,
    issue_run_dir,
    load_job_record,
    record_artifact_file,
    record_codex_token_usage,
    utc_iso,
    utc_timestamp,
    write_job_record,
    write_single_artifact,
    write_text_artifact,
)
from .locking import FileLock, LockHeld
from .models import DiffSummary, DiscussionComment, Issue, JobRecord, VerifyResult
from .pr import build_pr_body, render_template
from .privacy import sanitize_user_paths
from .prompt import (
    build_prompt,
    build_repair_prompt,
    build_resume_summary_prompt,
    build_review_fix_prompt,
    build_review_prompt,
)
from .shell import run_cmd
from .verifier import format_verification_summary, run_verifier
from .worktree import (
    GitError,
    add_worktree,
    commit_all,
    ensure_worktree,
    push_branch,
    remove_worktree,
    unique_branch_name,
)
from .worktree import ensure_git_ok


EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_DEPENDENCY = 2
EXIT_GH = 3
EXIT_GIT = 4
EXIT_AGENT = 5
EXIT_VERIFY = 6
EXIT_PR = 7
EXIT_LOCK = 8

REVIEW_FINDING_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[(P[0-3])\]|(P[0-3])\s*[:\-])", re.MULTILINE
)
MAX_FOLLOW_UP_CHARS = 12_000
MAX_SUMMARY_CHARS = 6_000
WORKER_COMMENT_PREFIXES = (
    "Draft PR opened:",
    "Updated PR:",
    "Git worktree setup failed:",
    "Agent failed with exit code",
    "Verification failed",
    "Diff policy rejected the result:",
    "PR creation failed:",
    "PR update failed:",
    "Git commit/push failed:",
    "Code review still reports blocking findings after",
)


class DependencyError(RuntimeError):
    pass


class IssueDependencyClient(Protocol):
    def blocked_by(self, number: int) -> list[Issue]: ...


class RunOverrides:
    def __init__(self, model: str | None = None, reasoning: str | None = None):
        self.model = model
        self.reasoning = reasoning


@dataclass(frozen=True)
class IssueWorkPlan:
    issue: Issue
    base_branch: str
    stack_depth: int = 0
    blocker_issue_numbers: list[int] | None = None
    mode: str = "new"
    resume_record: JobRecord | None = None


def apply_overrides(
    config: WorkerConfig, overrides: RunOverrides | None = None
) -> WorkerConfig:
    if overrides is None:
        return config
    if overrides.model:
        config.agent.model = overrides.model
    if overrides.reasoning:
        config.agent.reasoning = overrides.reasoning
    return config


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else root / path


def configured_paths(config: WorkerConfig, root: Path) -> dict[str, Path]:
    return {
        "worktree_root": _path(root, config.paths.worktree_root),
        "run_root": _path(root, config.paths.run_root),
        "log_root": _path(root, config.paths.log_root),
        "runtime_root": _path(root, config.paths.runtime_root),
    }


def _relative_prefix(root: Path, path: Path) -> str | None:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return None


def check_dependencies(
    config: WorkerConfig, root: Path | None = None, paths: dict[str, Path] | None = None
) -> None:
    checks = [
        ["gh", "--version"],
        ["git", "--version"],
        [*shlex.split(config.agent.command), "--version"],
    ]
    if config.review.enabled:
        checks.append([*shlex.split(config.review.command), "--version"])
    for check in checks:
        result = run_cmd(check)
        if result.exit_code != 0:
            raise DependencyError(
                result.stderr.strip() or f"dependency check failed: {result.command}"
            )
    GHClient(config.repo).validate()
    allowed_prefixes: list[str] = []
    if root and paths:
        for key in ("worktree_root", "run_root", "log_root", "runtime_root"):
            prefix = _relative_prefix(root, paths[key])
            if prefix:
                allowed_prefixes.append(prefix)
    ensure_git_ok(
        config.base_branch,
        allow_dirty=config.git.allow_dirty_base,
        allowed_dirty_prefixes=allowed_prefixes,
    )


def _comment_failure(gh: GHClient, issue: Issue, run_dir: Path, message: str) -> None:
    comment = run_dir / "failure-comment.md"
    write_single_artifact(comment, sanitize_user_paths(message))
    try:
        gh.comment(issue.number, comment)
    except GHError:
        pass


def _finalize_issue_failure(
    gh: GHClient,
    config: WorkerConfig,
    issue: Issue,
    run_dir: Path,
    message: str,
    add_failed_label: bool = True,
) -> None:
    if add_failed_label:
        try:
            gh.add_label(issue.number, config.issue_selection.failed_label)
        except GHError:
            pass
    try:
        gh.remove_label(issue.number, config.issue_selection.working_label)
    except GHError:
        pass
    _comment_failure(gh, issue, run_dir, message)


def _write_record(
    run_dir: Path, record: JobRecord, status: str, error: str | None = None
) -> None:
    record.status = status
    record.error_summary = error
    record.finished_at = utc_iso()
    write_job_record(run_dir, record)


def _record_for(
    issue: Issue,
    branch: str,
    worktree_path: Path,
    plan: IssueWorkPlan,
    pr_url: str | None = None,
) -> JobRecord:
    return JobRecord(
        issue_number=issue.number,
        issue_title=issue.title,
        branch_name=branch,
        worktree_path=str(worktree_path),
        status="selected",
        started_at=utc_iso(),
        base_branch=plan.base_branch,
        stack_depth=plan.stack_depth,
        blocker_issue_numbers=list(plan.blocker_issue_numbers or []),
        finished_at=None,
        pr_url=pr_url,
        error_summary=None,
        changed_files=[],
        verifier_passed=None,
    )


def _parse_iso_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        return None


def _resume_cutoff(record: JobRecord) -> datetime | None:
    return _parse_iso_timestamp(record.finished_at) or _parse_iso_timestamp(
        record.started_at
    )


def _is_newer_comment(comment: DiscussionComment, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    created = _parse_iso_timestamp(comment.created_at)
    return created is None or created > cutoff


def _is_worker_comment(comment: DiscussionComment) -> bool:
    body = comment.body.strip()
    return any(body.startswith(prefix) for prefix in WORKER_COMMENT_PREFIXES)


def _render_follow_up_comment(comment: DiscussionComment) -> str:
    meta_parts = [comment.source]
    if comment.author:
        meta_parts.append(f"by {comment.author}")
    if comment.created_at:
        meta_parts.append(f"at {comment.created_at}")
    meta = ", ".join(meta_parts)
    body = comment.body.strip()
    if comment.url:
        meta = f"{meta} ({comment.url})"
    return f"### {meta}\n\n{body}"


def _build_follow_up(
    pr_url: str,
    manual_note: str,
    previous_summary: str,
    comments: list[DiscussionComment],
) -> str:
    sections = [f"Continue work on the existing pull request for this issue: {pr_url}"]
    if previous_summary.strip():
        sections.append(
            "## Prior implementation summary\n\n"
            + previous_summary.strip()[:MAX_SUMMARY_CHARS]
        )
    if manual_note.strip():
        sections.append(f"### Local operator note\n\n{manual_note.strip()}")

    rendered_comments = [
        _render_follow_up_comment(comment)
        for comment in comments
        if comment.body.strip()
    ]
    if rendered_comments:
        kept: list[str] = []
        total = 0
        for section in reversed(rendered_comments):
            if total and total + len(section) > MAX_FOLLOW_UP_CHARS:
                break
            kept.append(section)
            total += len(section)
        kept.reverse()
        if len(kept) != len(rendered_comments):
            sections.append(
                "New GitHub discussion since the last worker run is truncated to the newest entries."
            )
        sections.append(
            "## New GitHub discussion since the last worker run\n\n" + "\n\n".join(kept)
        )

    return "\n\n".join(section for section in sections if section.strip()).strip()


def _open_blockers(gh: IssueDependencyClient, issue: Issue) -> list[Issue]:
    return [
        blocker
        for blocker in gh.blocked_by(issue.number)
        if blocker.state.lower() == "open"
    ]


def _latest_pr_job(paths: dict[str, Path], issue_number: int) -> JobRecord | None:
    latest = issue_run_dir(paths["run_root"], issue_number) / "latest.json"
    if not latest.exists():
        return None
    try:
        record = load_job_record(latest)
    except (OSError, ValueError, TypeError):
        return None
    if record.status != "pr_opened" or not record.pr_url:
        return None
    return record


def _latest_resume_job(paths: dict[str, Path], issue_number: int) -> JobRecord | None:
    latest = issue_run_dir(paths["run_root"], issue_number) / "latest.json"
    if not latest.exists():
        return None
    try:
        record = load_job_record(latest)
    except (OSError, ValueError, TypeError):
        return None
    if not record.branch_name or not record.pr_url:
        return None
    return record


def _latest_summary(paths: dict[str, Path], issue_number: int) -> str:
    summary_path = issue_run_dir(paths["run_root"], issue_number) / "summary.md"
    if not summary_path.exists():
        return ""
    try:
        return summary_path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _resume_summary_context(paths: dict[str, Path], previous_record: JobRecord) -> str:
    # Only reuse a stored summary when the latest recorded state matches the
    # last PR-opened snapshot. Failed resume attempts may leave a worktree that
    # has drifted beyond that summary.
    if previous_record.status != "pr_opened":
        return ""
    return _latest_summary(paths, previous_record.issue_number)


def _resume_discussion(
    gh: GHClient,
    issue_number: int,
    previous_record: JobRecord,
    previous_summary: str,
    manual_note: str = "",
) -> str:
    cutoff = _resume_cutoff(previous_record)
    comments = [
        *gh.issue_comments(issue_number),
        *gh.pr_comments(previous_record.pr_url or ""),
        *gh.pr_reviews(previous_record.pr_url or ""),
    ]
    fresh = [
        comment
        for comment in comments
        if not _is_worker_comment(comment) and _is_newer_comment(comment, cutoff)
    ]
    fresh.sort(key=lambda comment: comment.created_at or "")
    return _build_follow_up(
        previous_record.pr_url or "", manual_note, previous_summary, fresh
    )


def _failure_status(failure_kind: str) -> str:
    if failure_kind == "agent":
        return "agent_failed"
    if failure_kind == "review":
        return "review_failed"
    return "verify_failed"


def _work_plan_for_issue(
    gh: IssueDependencyClient,
    issue: Issue,
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> IssueWorkPlan | None:
    if config.parent_label in issue.labels:
        return IssueWorkPlan(issue, base_branch, mode="parent")
    if not config.respect_issue_dependencies:
        return IssueWorkPlan(issue, base_branch)

    blockers = _open_blockers(gh, issue)
    if not blockers:
        return IssueWorkPlan(issue, base_branch)
    if not config.allow_stacked_prs or len(blockers) != 1:
        return None

    blocker = blockers[0]
    blocker_job = _latest_pr_job(paths, blocker.number)
    if blocker_job is None:
        return None
    stack_depth = blocker_job.stack_depth + 1
    if stack_depth > config.max_stack_depth:
        return None
    return IssueWorkPlan(issue, blocker_job.branch_name, stack_depth, [blocker.number])


def _resume_plan_for_issue(
    issue: Issue,
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> IssueWorkPlan | None:
    labels = set(issue.labels)
    blocked = {config.working_label, config.failed_label, *config.blocked_labels}
    if (
        issue.state.lower() != "open"
        or config.resume_label not in labels
        or config.pr_opened_label not in labels
        or (labels & blocked)
    ):
        return None
    record = _latest_resume_job(paths, issue.number)
    if record is None:
        return None
    return IssueWorkPlan(
        issue=issue,
        base_branch=record.base_branch or base_branch,
        stack_depth=record.stack_depth,
        blocker_issue_numbers=record.blocker_issue_numbers,
        mode="resume",
        resume_record=record,
    )


def workable_issue_plans(
    gh: IssueDependencyClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> list[IssueWorkPlan]:
    candidates = candidate_issues(issues, config)
    plans: list[IssueWorkPlan] = []
    for issue in candidates:
        plan = _resume_plan_for_issue(issue, config, base_branch, paths)
        if plan is None:
            plan = _work_plan_for_issue(gh, issue, config, base_branch, paths)
        if plan is not None:
            plans.append(plan)
    return plans


def workable_issues(
    gh: IssueDependencyClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> list[Issue]:
    return [
        plan.issue
        for plan in workable_issue_plans(gh, issues, config, base_branch, paths)
    ]


def select_work_plan(
    gh: IssueDependencyClient,
    issues: list[Issue],
    config: IssueSelectionConfig,
    base_branch: str,
    paths: dict[str, Path],
) -> IssueWorkPlan | None:
    plans = workable_issue_plans(gh, issues, config, base_branch, paths)
    return plans[0] if plans else None


def blocking_review_priorities(
    review_output: str, blocking_priorities: list[str]
) -> list[str]:
    blocking = set(blocking_priorities)
    for line in review_output.splitlines():
        if line.strip().upper().startswith("BLOCKING_PRIORITIES:"):
            value = line.split(":", 1)[1].strip().upper()
            if value in {"", "NONE", "NO", "N/A"}:
                return []
            found = [
                priority
                for priority in re.findall(r"P[0-3]", value)
                if priority in blocking
            ]
            return sorted(set(found), key=found.index)

    matches: list[str] = []
    for match in REVIEW_FINDING_RE.finditer(review_output):
        priority = match.group(1) or match.group(2)
        if priority in blocking and priority not in matches:
            matches.append(priority)
    return matches


def _run_codex_session(
    config: WorkerConfig,
    worktree_path: Path,
    prompt_path: Path,
    log_path: Path,
    command: str | None = None,
):
    backend = CodexBackend(
        command or config.agent.command,
        log_path,
        model=config.agent.model,
        reasoning=config.agent.reasoning,
    )
    result = backend.run(
        worktree_path, prompt_path, timeout_sec=config.agent.timeout_minutes * 60
    )
    if log_path.exists():
        record_artifact_file(log_path)
        copy_artifact(log_path, log_path.parent / "codex.log")
        record_codex_token_usage(log_path.parent, log_path)
    return result


def _diff_snapshot(worktree_path: Path) -> str:
    status = run_cmd(["git", "status", "--porcelain"], cwd=worktree_path)
    result = run_cmd(["git", "diff", "--binary", "HEAD"], cwd=worktree_path)
    untracked = run_cmd(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"], cwd=worktree_path
    )
    ignored = run_cmd(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=worktree_path,
    )
    status_text = status.stdout if status.exit_code == 0 else ""
    diff_text = result.stdout if result.exit_code == 0 else ""
    untracked_parts: list[str] = []
    untracked_names: set[str] = set()
    if untracked.exit_code == 0:
        untracked_names.update(path for path in untracked.stdout.split("\0") if path)
    if ignored.exit_code == 0:
        untracked_names.update(path for path in ignored.stdout.split("\0") if path)
    for name in sorted(untracked_names):
        path = worktree_path / name
        try:
            digest = (
                hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
            )
        except OSError:
            digest = ""
        untracked_parts.append(f"{name}\0{digest}")
    return f"{status_text}\0{diff_text}\0{chr(0).join(untracked_parts)}"


def _run_verification_with_repairs(
    config: WorkerConfig,
    issue: Issue,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
) -> tuple[bool, VerifyResult, DiffSummary, str, str]:
    verify_log = run_dir / f"verify-{stamp}.log"
    verify = run_verifier(config.verify, worktree_path, verify_log)
    record_artifact_file(verify_log)
    latest_verify = run_dir / "verify.log"
    copy_artifact(verify_log, latest_verify)
    diff = inspect_diff(worktree_path, config.diff_policy)

    repairs = 0
    while not verify.passed and repairs < config.agent.max_repair_attempts:
        repairs += 1
        repair_stamp = f"{stamp}-repair-{repairs}"
        repair_prompt = build_repair_prompt(
            issue, verify_log.read_text(encoding="utf-8"), diff
        )
        repair_prompt_path = run_dir / f"prompt-{repair_stamp}.md"
        write_text_artifact(repair_prompt_path, run_dir / "prompt.md", repair_prompt)
        repair = _run_codex_session(
            config,
            worktree_path,
            repair_prompt_path,
            run_dir / f"codex-{repair_stamp}.log",
        )
        if not repair.success:
            return (
                False,
                verify,
                diff,
                f"Repair attempt failed with exit code {repair.exit_code}.",
                "agent",
            )
        verify_log = run_dir / f"verify-{repair_stamp}.log"
        verify = run_verifier(config.verify, worktree_path, verify_log)
        record_artifact_file(verify_log)
        copy_artifact(verify_log, latest_verify)
        diff = inspect_diff(worktree_path, config.diff_policy)

    return (
        verify.passed,
        verify,
        diff,
        "" if verify.passed else "Verification failed.",
        "verify",
    )


def _run_review_loop(
    config: WorkerConfig,
    issue: Issue,
    repo_root: Path,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
    verify: VerifyResult,
    diff: DiffSummary,
) -> tuple[bool, VerifyResult, DiffSummary, str, str]:
    if not config.review.enabled:
        return True, verify, diff, "", ""

    fixes_completed = 0
    review_iteration = 0
    while True:
        review_iteration += 1
        review_stamp = f"{stamp}-review-{review_iteration}"
        review_prompt = build_review_prompt(issue, config, repo_root, diff, verify)
        review_prompt_path = run_dir / f"prompt-{review_stamp}.md"
        write_text_artifact(review_prompt_path, run_dir / "prompt.md", review_prompt)
        before_review = _diff_snapshot(worktree_path)
        review = _run_codex_session(
            config,
            worktree_path,
            review_prompt_path,
            run_dir / f"codex-{review_stamp}.log",
            command=config.review.command,
        )
        after_review = _diff_snapshot(worktree_path)
        review_output = (review.stdout.strip() or review.stderr.strip()).strip()
        write_text_artifact(
            run_dir / f"review-{review_stamp}.md", run_dir / "review.md", review_output
        )
        if not review.success:
            return (
                False,
                verify,
                diff,
                f"Review attempt failed with exit code {review.exit_code}.",
                "agent",
            )
        if after_review != before_review:
            return (
                False,
                verify,
                diff,
                "Review attempt modified the worktree; review sessions must be read-only.",
                "agent",
            )
        if not review_output:
            return (
                False,
                verify,
                diff,
                "Review attempt completed without output.",
                "agent",
            )

        priorities = blocking_review_priorities(
            review_output, config.review.fix_priorities
        )
        if not priorities:
            return True, verify, diff, "", ""

        if fixes_completed >= config.review.max_iterations:
            return (
                False,
                verify,
                diff,
                "Code review still reports blocking findings after "
                f"{config.review.max_iterations} fix iteration(s): {', '.join(priorities)}.\n\n"
                f"Latest review output:\n{review_output[-4000:]}",
                "review",
            )

        fixes_completed += 1
        fix_stamp = f"{stamp}-review-fix-{fixes_completed}"
        fix_prompt = build_review_fix_prompt(
            issue, review_output, diff, config.review.fix_priorities
        )
        fix_prompt_path = run_dir / f"prompt-{fix_stamp}.md"
        write_text_artifact(fix_prompt_path, run_dir / "prompt.md", fix_prompt)
        fix = _run_codex_session(
            config,
            worktree_path,
            fix_prompt_path,
            run_dir / f"codex-{fix_stamp}.log",
        )
        if not fix.success:
            return (
                False,
                verify,
                diff,
                f"Review fix attempt failed with exit code {fix.exit_code}.",
                "agent",
            )

        ok, verify, diff, error, failure_kind = _run_verification_with_repairs(
            config,
            issue,
            worktree_path,
            run_dir,
            fix_stamp,
        )
        if not ok:
            return (
                False,
                verify,
                diff,
                f"Verification failed after review fix.\n\n{error}",
                failure_kind,
            )


def _run_agent_and_verify(
    config: WorkerConfig,
    issue: Issue,
    repo_root: Path,
    worktree_path: Path,
    run_dir: Path,
    stamp: str,
    follow_up: str = "",
) -> tuple[bool, VerifyResult | None, DiffSummary, str, str]:
    prompt_text = build_prompt(issue, config, repo_root, follow_up=follow_up)
    prompt_path = run_dir / f"prompt-{stamp}.md"
    write_text_artifact(prompt_path, run_dir / "prompt.md", prompt_text)

    codex_log = run_dir / f"codex-{stamp}.log"
    agent = _run_codex_session(config, worktree_path, prompt_path, codex_log)
    if not agent.success:
        return (
            False,
            None,
            inspect_diff(worktree_path, config.diff_policy),
            (
                f"Agent failed with exit code {agent.exit_code}."
                f"\n\nstdout:\n{agent.stdout[-2000:]}\n\nstderr:\n{agent.stderr[-2000:]}"
            ),
            "agent",
        )

    ok, verify, diff, error, failure_kind = _run_verification_with_repairs(
        config, issue, worktree_path, run_dir, stamp
    )
    if not ok:
        return ok, verify, diff, error, failure_kind

    return _run_review_loop(
        config, issue, repo_root, worktree_path, run_dir, stamp, verify, diff
    )


def _write_resume_summary(
    config: WorkerConfig,
    issue: Issue,
    run_dir: Path,
    stamp: str,
    diff: DiffSummary,
    verify: VerifyResult,
    previous_summary: str = "",
) -> str:
    summary_prompt = build_resume_summary_prompt(
        issue, diff, verify, previous_summary=previous_summary
    )
    summary_prompt_path = run_dir / f"prompt-{stamp}-summary.md"
    write_text_artifact(summary_prompt_path, run_dir / "prompt.md", summary_prompt)
    summary_command = config.review.command if config.review.enabled else None
    summary = _run_codex_session(
        config,
        run_dir,
        summary_prompt_path,
        run_dir / f"codex-{stamp}-summary.log",
        command=summary_command,
    )
    summary_output = (summary.stdout.strip() or summary.stderr.strip()).strip()
    if not summary.success:
        append_artifact_log(
            run_dir,
            f"resume summary generation failed with exit code {summary.exit_code}",
        )
        return ""
    if not summary_output:
        append_artifact_log(
            run_dir, "resume summary generation completed without output"
        )
        return ""
    summary_path = run_dir / f"summary-{stamp}.md"
    write_text_artifact(summary_path, run_dir / "summary.md", summary_output)
    return summary_output


def _finalize_pr_success(
    gh: GHClient,
    config: WorkerConfig,
    issue: Issue,
    run_dir: Path,
    record: JobRecord,
    pr_url: str,
    success_message: str,
    remove_ready_label: bool,
) -> None:
    record.pr_url = pr_url
    try:
        gh.add_label(issue.number, config.issue_selection.pr_opened_label)
        gh.remove_label(issue.number, config.issue_selection.working_label)
        try:
            gh.remove_label(issue.number, config.issue_selection.resume_label)
        except GHError:
            pass
        try:
            gh.remove_label(issue.number, config.issue_selection.failed_label)
        except GHError:
            pass
        if remove_ready_label and config.git.remove_ready_on_pr:
            gh.remove_label(issue.number, config.issue_selection.ready_label)
        comment = run_dir / "success-comment.md"
        write_single_artifact(comment, f"{success_message}: {pr_url}\n")
        gh.comment(issue.number, comment)
    except GHError:
        pass
    _write_record(run_dir, record, "pr_opened", None)


def _parent_record(parent: Issue, paths: dict[str, Path]) -> JobRecord:
    return JobRecord(
        issue_number=parent.number,
        issue_title=parent.title,
        branch_name="",
        worktree_path=str(paths["worktree_root"] / f"parent-{parent.number}"),
        status="selected",
        started_at=utc_iso(),
        base_branch=None,
        stack_depth=0,
        blocker_issue_numbers=[],
    )


def _child_done(
    child: Issue, config: IssueSelectionConfig, processed: set[int]
) -> bool:
    return (
        child.number in processed
        or child.state.lower() == "closed"
        or config.pr_opened_label in child.labels
    )


def _child_runnable(
    child: Issue, config: IssueSelectionConfig, processed: set[int]
) -> bool:
    labels = set(child.labels)
    return (
        child.number not in processed
        and child.state.lower() == "open"
        and config.pr_opened_label not in labels
        and config.failed_label not in labels
        and config.working_label not in labels
        and not (labels & set(config.blocked_labels))
    )


def _parent_plan_data(gh: GHClient, parent: Issue, children: list[Issue]) -> dict:
    child_items = []
    for child in children:
        try:
            blockers = gh.blocked_by(child.number)
        except GHError:
            blockers = []
        child_items.append(
            {
                "number": child.number,
                "title": child.title,
                "state": child.state,
                "labels": child.labels,
                "blocked_by": [blocker.number for blocker in blockers],
            }
        )
    return {
        "parent": {
            "number": parent.number,
            "title": parent.title,
            "state": parent.state,
            "labels": parent.labels,
        },
        "children": child_items,
    }


def _write_parent_plan(
    run_dir: Path, gh: GHClient, parent: Issue, children: list[Issue]
) -> None:
    content = (
        json.dumps(_parent_plan_data(gh, parent, children), indent=2, sort_keys=True)
        + "\n"
    )
    write_text_artifact(
        run_dir / f"parent-plan-{utc_timestamp()}.json",
        run_dir / "parent-plan.json",
        content,
    )


def _parent_memory_context(
    parent: Issue, children: list[Issue], paths: dict[str, Path], run_dir: Path
) -> str:
    sections = [
        f"Continue a child issue that belongs to parent issue #{parent.number}: {parent.title}",
        "## Parent issue body\n\n" + parent.body.strip(),
    ]
    plan_path = run_dir / "parent-plan.json"
    if plan_path.exists():
        sections.append(
            "## Parent DAG snapshot\n\n```json\n"
            + plan_path.read_text(encoding="utf-8")[:MAX_FOLLOW_UP_CHARS]
            + "\n```"
        )
    memory_path = run_dir / "parent-memory.md"
    if memory_path.exists():
        memory = memory_path.read_text(encoding="utf-8").strip()
        if memory:
            sections.append("## Parent memory\n\n" + memory[-MAX_FOLLOW_UP_CHARS:])
    child_summaries = []
    for child in children:
        summary = _latest_summary(paths, child.number)
        record = _latest_pr_job(paths, child.number)
        if summary or record:
            pr = f"\nPR: {record.pr_url}" if record and record.pr_url else ""
            child_summaries.append(
                f"### Child #{child.number}: {child.title}{pr}\n\n{summary}"
            )
    if child_summaries:
        sections.append(
            "## Completed child summaries\n\n"
            + "\n\n".join(child_summaries)[-MAX_FOLLOW_UP_CHARS:]
        )
    return "\n\n".join(section for section in sections if section.strip()).strip()


def _append_parent_memory(
    run_dir: Path, child: Issue, pr_url: str | None, summary: str
) -> None:
    memory_path = run_dir / "parent-memory.md"
    existing = memory_path.read_text(encoding="utf-8") if memory_path.exists() else ""
    pr_line = f"\nPR: {pr_url}" if pr_url else ""
    entry = f"## Child #{child.number}: {child.title}{pr_line}\n\n{summary.strip() or 'No summary was generated.'}\n\n"
    write_text_artifact(
        run_dir / f"parent-memory-{utc_timestamp()}.md",
        memory_path,
        (existing + entry).strip() + "\n",
    )


def _finish_parent_success(
    gh: GHClient,
    config: WorkerConfig,
    parent: Issue,
    run_dir: Path,
    record: JobRecord,
    processed_children: list[Issue],
) -> None:
    try:
        try:
            gh.remove_label(parent.number, config.issue_selection.working_label)
        except GHError:
            pass
        try:
            gh.remove_label(parent.number, config.issue_selection.ready_label)
        except GHError:
            pass
        gh.add_label(parent.number, config.issue_selection.parent_done_label)
        comment = run_dir / "parent-success-comment.md"
        lines = ["Parent orchestration opened all available child pull requests."]
        for child in processed_children:
            child_record = _latest_pr_job({"run_root": run_dir.parent}, child.number)
            if child_record and child_record.pr_url:
                lines.append(f"- #{child.number}: {child_record.pr_url}")
        write_single_artifact(comment, "\n".join(lines) + "\n")
        gh.comment(parent.number, comment)
    except GHError:
        pass
    _write_record(run_dir, record, "parent_done", None)


def _finish_parent_waiting(
    gh: GHClient,
    config: WorkerConfig,
    parent: Issue,
    run_dir: Path,
    record: JobRecord,
    status: str,
) -> None:
    try:
        gh.remove_label(parent.number, config.issue_selection.working_label)
    except GHError:
        pass
    _write_record(run_dir, record, status, None)


def process_parent_issue(
    config: WorkerConfig, plan: IssueWorkPlan, repo_root: Path, paths: dict[str, Path]
) -> int:
    parent = plan.issue
    gh = GHClient(config.repo)
    stamp = utc_timestamp()
    run_dir = issue_run_dir(paths["run_root"], parent.number)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = _parent_record(parent, paths)
    write_job_record(run_dir, record, stamp)

    try:
        gh.add_label(parent.number, config.issue_selection.working_label)
        try:
            gh.remove_label(parent.number, config.issue_selection.failed_label)
        except GHError:
            pass
        record.status = "working"
        write_job_record(run_dir, record, stamp)
        children = gh.sub_issues(parent.number)
        _write_parent_plan(run_dir, gh, parent, children)
    except GHError as exc:
        _finalize_issue_failure(
            gh, config, parent, run_dir, f"Parent orchestration setup failed:\n\n{exc}"
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GH

    processed: set[int] = set()
    processed_children: list[Issue] = []
    attempts = 0
    while attempts < config.issue_selection.max_parent_children_per_run:
        next_plan: IssueWorkPlan | None = None
        next_child: Issue | None = None
        for child in children:
            if not _child_runnable(child, config.issue_selection, processed):
                continue
            child_plan = _work_plan_for_issue(
                gh, child, config.issue_selection, config.base_branch, paths
            )
            if child_plan is not None:
                next_plan = child_plan
                next_child = child
                break
        if next_plan is None or next_child is None:
            break

        follow_up = _parent_memory_context(parent, children, paths, run_dir)
        result = process_issue(config, next_plan, repo_root, paths, follow_up=follow_up)
        if result != EXIT_OK:
            message = f"Parent orchestration stopped because child issue #{next_child.number} failed with exit code {result}."
            _finalize_issue_failure(gh, config, parent, run_dir, message)
            _write_record(run_dir, record, "parent_failed", message)
            return result
        processed.add(next_child.number)
        processed_children.append(next_child)
        attempts += 1
        child_record = _latest_pr_job(paths, next_child.number)
        _append_parent_memory(
            run_dir,
            next_child,
            child_record.pr_url if child_record else None,
            _latest_summary(paths, next_child.number),
        )
        _write_parent_plan(run_dir, gh, parent, children)

    if children and all(
        _child_done(child, config.issue_selection, processed) for child in children
    ):
        _finish_parent_success(gh, config, parent, run_dir, record, processed_children)
        return EXIT_OK

    status = "parent_partial" if processed else "parent_waiting"
    _finish_parent_waiting(gh, config, parent, run_dir, record, status)
    return EXIT_OK


def process_issue(
    config: WorkerConfig,
    plan: IssueWorkPlan,
    repo_root: Path,
    paths: dict[str, Path],
    follow_up: str = "",
) -> int:
    issue = plan.issue
    gh = GHClient(config.repo)
    stamp = utc_timestamp()
    branch = unique_branch_name(config.git, issue.number, issue.title)
    worktree_path = paths["worktree_root"] / f"issue-{issue.number}"
    run_dir = issue_run_dir(paths["run_root"], issue.number)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = _record_for(issue, branch, worktree_path, plan)
    write_job_record(run_dir, record, stamp)

    gh.add_label(issue.number, config.issue_selection.working_label)
    record.status = "working"
    write_job_record(run_dir, record, stamp)

    try:
        add_worktree(worktree_path, branch, plan.base_branch)
    except GitError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"Git worktree setup failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        issue,
        repo_root,
        worktree_path,
        run_dir,
        stamp,
        follow_up=follow_up,
    )
    record.changed_files = diff.changed_files
    record.verifier_passed = verify.passed if verify else None
    if not ok:
        status = _failure_status(failure_kind)
        _finalize_issue_failure(
            gh, config, issue, run_dir, error, add_failed_label=True
        )
        _write_record(run_dir, record, status, error)
        return EXIT_AGENT if failure_kind == "agent" else EXIT_VERIFY

    if diff.rejected:
        status = (
            "no_changes" if diff.rejection_reason == "no changes" else "diff_rejected"
        )
        message = f"Diff policy rejected the result:\n\n{diff.rejection_reason}"
        _finalize_issue_failure(
            gh, config, issue, run_dir, message, add_failed_label=True
        )
        _write_record(run_dir, record, status, diff.rejection_reason)
        return EXIT_VERIFY

    try:
        commit_message = render_template(config.git.commit_message_template, issue)
        commit_all(worktree_path, commit_message)
        record.status = "committed"
        write_job_record(run_dir, record, stamp)
        push_branch(worktree_path, branch)
        record.status = "pushed"
        write_job_record(run_dir, record, stamp)
    except GitError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"Git commit/push failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    assert verify is not None
    verification_summary = format_verification_summary(verify)
    pr_body = build_pr_body(config.pr, issue, verification_summary, diff)
    pr_body_path = run_dir / f"pr-body-{stamp}.md"
    write_text_artifact(pr_body_path, run_dir / "pr_body.md", pr_body)
    try:
        pr_title = render_template(config.pr.title_template, issue)
        pr_url = gh.create_pr(
            plan.base_branch, branch, pr_title, pr_body_path, draft=config.pr.draft
        )
    except GHError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"PR creation failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_PR

    _finalize_pr_success(
        gh,
        config,
        issue,
        run_dir,
        record,
        pr_url,
        success_message="Draft PR opened",
        remove_ready_label=True,
    )
    _write_resume_summary(config, issue, run_dir, stamp, diff, verify)
    if not config.git.keep_worktree_on_success:
        try:
            remove_worktree(worktree_path)
        except GitError:
            pass
    return EXIT_OK


def resume_issue(
    config_path: Path,
    issue_number: int,
    manual_note: str = "",
    repo_root: Path | None = None,
    overrides: RunOverrides | None = None,
) -> int:
    root = repo_root or Path.cwd()
    try:
        config = apply_overrides(load_config(config_path), overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    try:
        with FileLock(paths["runtime_root"] / "worker.lock"):
            try:
                check_dependencies(config, root=root, paths=paths)
            except DependencyError as exc:
                print(f"dependency error: {exc}")
                return EXIT_DEPENDENCY
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
            except GitError as exc:
                print(f"git error: {exc}")
                return EXIT_GIT

            gh = GHClient(config.repo)
            previous_record = _latest_resume_job(paths, issue_number)
            if previous_record is None or not previous_record.pr_url:
                print(
                    f"resume error: issue #{issue_number} does not have a recorded open PR in local worker state"
                )
                return EXIT_CONFIG
            try:
                issue = gh.view_issue(issue_number)
                return process_issue_resume(
                    config, issue, previous_record, root, paths, manual_note
                )
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
    except LockHeld:
        print("lock already held")
        return EXIT_LOCK


def process_issue_resume(
    config: WorkerConfig,
    issue: Issue,
    previous_record: JobRecord,
    repo_root: Path,
    paths: dict[str, Path],
    manual_note: str = "",
) -> int:
    gh = GHClient(config.repo)
    stamp = utc_timestamp()
    pr_url = previous_record.pr_url
    assert pr_url is not None
    branch = previous_record.branch_name
    worktree_path = Path(previous_record.worktree_path)
    run_dir = issue_run_dir(paths["run_root"], issue.number)
    run_dir.mkdir(parents=True, exist_ok=True)
    previous_summary = _resume_summary_context(paths, previous_record)
    record = _record_for(
        issue,
        branch,
        worktree_path,
        IssueWorkPlan(
            issue=issue,
            base_branch=previous_record.base_branch or config.base_branch,
            stack_depth=previous_record.stack_depth,
            blocker_issue_numbers=previous_record.blocker_issue_numbers,
        ),
        pr_url=pr_url,
    )
    write_job_record(run_dir, record, stamp)

    follow_up = _resume_discussion(
        gh, issue.number, previous_record, previous_summary, manual_note
    )

    try:
        gh.add_label(issue.number, config.issue_selection.working_label)
        try:
            gh.remove_label(issue.number, config.issue_selection.failed_label)
        except GHError:
            pass
        record.status = "working"
        write_job_record(run_dir, record, stamp)
    except GHError:
        pass

    try:
        ensure_worktree(worktree_path, branch)
    except GitError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"Git worktree setup failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        issue,
        repo_root,
        worktree_path,
        run_dir,
        stamp,
        follow_up=follow_up,
    )
    record.changed_files = diff.changed_files
    record.verifier_passed = verify.passed if verify else None
    if not ok:
        status = _failure_status(failure_kind)
        _finalize_issue_failure(
            gh, config, issue, run_dir, error, add_failed_label=True
        )
        _write_record(run_dir, record, status, error)
        return EXIT_AGENT if failure_kind == "agent" else EXIT_VERIFY

    if diff.rejected:
        status = (
            "no_changes" if diff.rejection_reason == "no changes" else "diff_rejected"
        )
        message = f"Diff policy rejected the result:\n\n{diff.rejection_reason}"
        _finalize_issue_failure(
            gh, config, issue, run_dir, message, add_failed_label=True
        )
        _write_record(run_dir, record, status, diff.rejection_reason)
        return EXIT_VERIFY

    try:
        commit_message = render_template(config.git.commit_message_template, issue)
        commit_all(worktree_path, commit_message)
        record.status = "committed"
        write_job_record(run_dir, record, stamp)
        push_branch(worktree_path, branch)
        record.status = "pushed"
        write_job_record(run_dir, record, stamp)
    except GitError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"Git commit/push failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_GIT

    assert verify is not None
    verification_summary = format_verification_summary(verify)
    pr_body = build_pr_body(config.pr, issue, verification_summary, diff)
    pr_body_path = run_dir / f"pr-body-{stamp}.md"
    write_text_artifact(pr_body_path, run_dir / "pr_body.md", pr_body)
    try:
        pr_title = render_template(config.pr.title_template, issue)
        gh.update_pr(pr_url, pr_title, pr_body_path)
    except GHError as exc:
        _finalize_issue_failure(
            gh,
            config,
            issue,
            run_dir,
            f"PR update failed:\n\n{exc}",
            add_failed_label=False,
        )
        _write_record(run_dir, record, "failed", str(exc))
        return EXIT_PR

    _finalize_pr_success(
        gh,
        config,
        issue,
        run_dir,
        record,
        pr_url,
        success_message="Updated PR",
        remove_ready_label=False,
    )
    _write_resume_summary(
        config,
        issue,
        run_dir,
        stamp,
        diff,
        verify,
        previous_summary=previous_summary,
    )
    if not config.git.keep_worktree_on_success:
        try:
            remove_worktree(worktree_path)
        except GitError:
            pass
    return EXIT_OK


def run_once(
    config_path: Path,
    repo_root: Path | None = None,
    overrides: RunOverrides | None = None,
) -> int:
    root = repo_root or Path.cwd()
    try:
        config = apply_overrides(load_config(config_path), overrides)
    except ConfigError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)

    try:
        with FileLock(paths["runtime_root"] / "worker.lock"):
            try:
                check_dependencies(config, root=root, paths=paths)
            except DependencyError as exc:
                print(f"dependency error: {exc}")
                return EXIT_DEPENDENCY
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
            except GitError as exc:
                print(f"git error: {exc}")
                return EXIT_GIT

            gh = GHClient(config.repo)
            try:
                issues = gh.list_issues(
                    [
                        config.issue_selection.ready_label,
                        config.issue_selection.resume_label,
                    ]
                )
                plan = select_work_plan(
                    gh, issues, config.issue_selection, config.base_branch, paths
                )
                if plan is None:
                    return EXIT_OK
                issue = gh.view_issue(plan.issue.number)
                plan = IssueWorkPlan(
                    issue=issue,
                    base_branch=plan.base_branch,
                    stack_depth=plan.stack_depth,
                    blocker_issue_numbers=plan.blocker_issue_numbers,
                    mode=plan.mode,
                    resume_record=plan.resume_record,
                )
                if plan.mode == "resume":
                    assert plan.resume_record is not None
                    return process_issue_resume(
                        config, issue, plan.resume_record, root, paths
                    )
                if plan.mode == "parent":
                    return process_parent_issue(config, plan, root, paths)
                return process_issue(config, plan, root, paths)
            except GHError as exc:
                print(f"GitHub error: {exc}")
                return EXIT_GH
    except LockHeld:
        print("lock already held")
        return EXIT_LOCK
