from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from pathlib import Path

from .config import GitConfig
from .shell import run_cmd


class GitError(RuntimeError):
    pass


def slugify_title(title: str, max_len: int = 50) -> str:
    normalized = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug[:max_len].rstrip("-") or "issue")


def branch_name(prefix: str, issue_number: int, title: str, max_length: int = 80, timestamp: str | None = None) -> str:
    suffix = f"{issue_number}-{slugify_title(title, max_length)}"
    branch = f"{prefix}{suffix}"
    if len(branch) > max_length:
        branch = branch[:max_length].rstrip("-")
    if timestamp:
        stamp = f"-{timestamp}"
        branch = branch[: max_length - len(stamp)].rstrip("-") + stamp
    return branch


def _status_path(line: str) -> str:
    path = line[3:].strip()
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip('"')


def _is_allowed_dirty(path: str, allowed_prefixes: list[str]) -> bool:
    normalized = path.rstrip("/")
    for prefix in allowed_prefixes:
        clean = prefix.strip("/").rstrip("/")
        if clean and (normalized == clean or normalized.startswith(f"{clean}/")):
            return True
    return False


def ensure_git_ok(base_branch: str, allow_dirty: bool = False, allowed_dirty_prefixes: list[str] | None = None) -> None:
    top = run_cmd(["git", "rev-parse", "--show-toplevel"])
    if top.exit_code != 0:
        raise GitError(top.stderr.strip() or "not inside a git repository")
    remote = run_cmd(["git", "remote", "-v"])
    if remote.exit_code != 0:
        raise GitError(remote.stderr.strip() or "git remote check failed")
    status = run_cmd(["git", "status", "--porcelain"])
    if status.exit_code != 0:
        raise GitError(status.stderr.strip() or "git status failed")
    allowed = allowed_dirty_prefixes or []
    dirty = [line for line in status.stdout.splitlines() if not _is_allowed_dirty(_status_path(line), allowed)]
    if dirty and not allow_dirty:
        details = "\n".join(dirty[:20])
        raise GitError(f"base checkout has uncommitted changes:\n{details}")
    fetch = run_cmd(["git", "fetch", "origin", base_branch])
    if fetch.exit_code != 0:
        raise GitError(fetch.stderr.strip() or "git fetch failed")


def branch_exists(name: str) -> bool:
    local = run_cmd(["git", "rev-parse", "--verify", "--quiet", name])
    remote = run_cmd(["git", "ls-remote", "--exit-code", "--heads", "origin", name])
    return local.exit_code == 0 or remote.exit_code == 0


def local_branch_exists(name: str) -> bool:
    return run_cmd(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).exit_code == 0


def unique_branch_name(config: GitConfig, issue_number: int, title: str) -> str:
    name = branch_name(config.branch_prefix, issue_number, title)
    if not branch_exists(name):
        return name
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return branch_name(config.branch_prefix, issue_number, title, timestamp=stamp)


def add_worktree(path: Path, branch: str, base_branch: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fetch = run_cmd(["git", "fetch", "origin", base_branch])
    if fetch.exit_code != 0:
        raise GitError(fetch.stderr.strip() or "git fetch failed")
    result = run_cmd(["git", "worktree", "add", "-b", branch, str(path), f"origin/{base_branch}"])
    if result.exit_code != 0:
        raise GitError(result.stderr.strip() or "git worktree add failed")


def ensure_worktree(path: Path, branch: str) -> None:
    if path.exists():
        result = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        if result.exit_code != 0:
            raise GitError(result.stderr.strip() or "git branch check failed")
        current = result.stdout.strip()
        if current != branch:
            raise GitError(f"existing worktree at {path} is on branch {current}, expected {branch}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if local_branch_exists(branch):
        result = run_cmd(["git", "worktree", "add", str(path), branch])
        if result.exit_code != 0:
            raise GitError(result.stderr.strip() or "git worktree add failed")
        return

    fetch = run_cmd(["git", "fetch", "origin", branch])
    if fetch.exit_code != 0:
        raise GitError(fetch.stderr.strip() or "git fetch failed")
    result = run_cmd(["git", "worktree", "add", "-B", branch, str(path), f"origin/{branch}"])
    if result.exit_code != 0:
        raise GitError(result.stderr.strip() or "git worktree add failed")


def remove_worktree(path: Path) -> None:
    result = run_cmd(["git", "worktree", "remove", str(path)])
    if result.exit_code != 0:
        raise GitError(result.stderr.strip() or "git worktree remove failed")
    run_cmd(["git", "worktree", "prune"])


def changed_files(worktree_path: Path) -> list[str]:
    result = run_cmd(["git", "status", "--porcelain"], cwd=worktree_path)
    if result.exit_code != 0:
        raise GitError(result.stderr.strip() or "git status failed")
    files: list[str] = []
    for line in result.stdout.splitlines():
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path:
            files.append(path)
    return files


def commit_all(worktree_path: Path, message: str) -> None:
    add = run_cmd(["git", "add", "."], cwd=worktree_path)
    if add.exit_code != 0:
        raise GitError(add.stderr.strip() or "git add failed")
    commit = run_cmd(["git", "commit", "-m", message], cwd=worktree_path)
    if commit.exit_code != 0:
        raise GitError(commit.stderr.strip() or "git commit failed")


def push_branch(worktree_path: Path, branch: str) -> None:
    result = run_cmd(["git", "push", "-u", "origin", branch], cwd=worktree_path)
    if result.exit_code != 0:
        raise GitError(result.stderr.strip() or "git push failed")
