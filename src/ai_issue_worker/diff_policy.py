from __future__ import annotations

import fnmatch
from pathlib import Path

from .config import DiffPolicyConfig
from .models import DiffSummary
from .shell import run_cmd
from .worktree import changed_files


LOCKFILES = {
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(Path(path).name, pattern) for pattern in patterns)


def _diff_line_count(worktree_path: Path) -> int:
    result = run_cmd(["git", "diff", "--numstat", "HEAD"], cwd=worktree_path)
    if result.exit_code != 0:
        return 0
    count = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            try:
                count += int(parts[0]) + int(parts[1])
            except ValueError:
                continue
    return count


def inspect_diff(worktree_path: Path, config: DiffPolicyConfig) -> DiffSummary:
    files = changed_files(worktree_path)
    stat_result = run_cmd(["git", "diff", "--stat", "HEAD"], cwd=worktree_path)
    check_result = run_cmd(["git", "diff", "--check"], cwd=worktree_path)
    diff_stat = stat_result.stdout
    line_count = _diff_line_count(worktree_path)

    reason: str | None = None
    if not files:
        reason = "no changes"
    elif len(files) > config.max_changed_files:
        reason = f"changed file count {len(files)} exceeds limit {config.max_changed_files}"
    elif line_count > config.max_diff_lines:
        reason = f"diff line count {line_count} exceeds limit {config.max_diff_lines}"
    elif _matches(next((path for path in files if _matches(path, config.reject_paths)), ""), config.reject_paths):
        bad = next(path for path in files if _matches(path, config.reject_paths))
        reason = f"changed rejected path: {bad}"
    elif not config.allow_lockfile_changes:
        bad_lock = next((path for path in files if Path(path).name in LOCKFILES), None)
        if bad_lock:
            reason = f"lockfile changed: {bad_lock}"
    elif check_result.exit_code != 0:
        reason = "git diff --check failed"

    return DiffSummary(
        changed_files=files,
        diff_stat=diff_stat,
        diff_line_count=line_count,
        rejected=reason is not None,
        rejection_reason=reason,
    )

