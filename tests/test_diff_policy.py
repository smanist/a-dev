from pathlib import Path

from ai_issue_worker.config import DiffPolicyConfig
from ai_issue_worker.diff_policy import inspect_diff
from ai_issue_worker.shell import run_cmd


def init_repo(path: Path):
    run_cmd(["git", "init"], cwd=path)
    run_cmd(["git", "config", "user.email", "test@example.com"], cwd=path)
    run_cmd(["git", "config", "user.name", "Test User"], cwd=path)
    (path / "app.py").write_text("print('ok')\n", encoding="utf-8")
    run_cmd(["git", "add", "."], cwd=path)
    run_cmd(["git", "commit", "-m", "initial"], cwd=path)


def test_diff_policy_rejects_no_changes(tmp_path: Path):
    init_repo(tmp_path)
    diff = inspect_diff(tmp_path, DiffPolicyConfig())
    assert diff.rejected
    assert diff.rejection_reason == "no changes"


def test_diff_policy_rejects_too_many_files(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "a.py").write_text("a=1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b=1\n", encoding="utf-8")
    diff = inspect_diff(tmp_path, DiffPolicyConfig(max_changed_files=1))
    assert diff.rejected
    assert diff.rejection_reason is not None
    assert "changed file count" in diff.rejection_reason


def test_diff_policy_rejects_rejected_path(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / ".env").write_text("SECRET=x\n", encoding="utf-8")
    diff = inspect_diff(tmp_path, DiffPolicyConfig())
    assert diff.rejected
    assert diff.rejection_reason is not None
    assert "rejected path" in diff.rejection_reason


def test_diff_policy_rejects_lockfile_when_disabled(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "poetry.lock").write_text("# lock\n", encoding="utf-8")
    diff = inspect_diff(tmp_path, DiffPolicyConfig(allow_lockfile_changes=False))
    assert diff.rejected
    assert diff.rejection_reason is not None
    assert "lockfile" in diff.rejection_reason


def test_diff_policy_allows_small_source_and_test_changes(tmp_path: Path):
    init_repo(tmp_path)
    (tmp_path / "app.py").write_text("print('better')\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    diff = inspect_diff(tmp_path, DiffPolicyConfig())
    assert not diff.rejected
    assert sorted(diff.changed_files) == ["app.py", "test_app.py"]
