from pathlib import Path

import pytest  # type: ignore[reportMissingImports]

from ai_issue_worker.shell import run_cmd
from ai_issue_worker.worktree import GitError, ensure_git_ok


def init_remote_backed_repo(path: Path):
    remote = path / "remote.git"
    repo = path / "repo"
    run_cmd(["git", "init", "--bare", str(remote)])
    run_cmd(["git", "init", str(repo)])
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=repo)
    run_cmd(["git", "config", "user.name", "Test User"], cwd=repo)
    (repo / "app.py").write_text("print('ok')\n", encoding="utf-8")
    run_cmd(["git", "add", "."], cwd=repo)
    run_cmd(["git", "commit", "-m", "initial"], cwd=repo)
    run_cmd(["git", "branch", "-M", "main"], cwd=repo)
    run_cmd(["git", "remote", "add", "origin", str(remote)], cwd=repo)
    run_cmd(["git", "push", "-u", "origin", "main"], cwd=repo)
    return repo


def test_ensure_git_ok_ignores_worker_runtime_paths(tmp_path: Path, monkeypatch):
    repo = init_remote_backed_repo(tmp_path)
    monkeypatch.chdir(repo)
    runtime = repo / ".ai-runtime"
    runtime.mkdir()
    (runtime / "worker.lock").write_text("123\n", encoding="utf-8")
    ensure_git_ok("main", allowed_dirty_prefixes=[".ai-runtime"])


def test_ensure_git_ok_reports_real_dirty_paths(tmp_path: Path, monkeypatch):
    repo = init_remote_backed_repo(tmp_path)
    monkeypatch.chdir(repo)
    (repo / "dirty.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(GitError, match="dirty.py"):
        ensure_git_ok("main", allowed_dirty_prefixes=[".ai-runtime"])
