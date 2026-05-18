import json
import shutil
from pathlib import Path
from typing import Any

from ai_issue_worker import cli
from ai_issue_worker.config import load_config
from ai_issue_worker.models import CommandResult, CreatedIssue, Issue, PullRequest


class FakeGH:
    def __init__(self, repo: str):
        self.repo = repo

    def list_issues(self, labels):
        ready_label = labels[0] if isinstance(labels, list) else labels
        return [
            Issue(
                1, "Ready", "", [ready_label], "open", updated_at="2026-01-01T00:00:00Z"
            )
        ]

    def blocked_by(self, number: int):
        return []


def test_top_level_help_summarizes_commands():
    help_text = cli.build_parser().format_help()

    assert "init                bootstrap config, labels, and editor tasks" in help_text
    assert "run-once            process one eligible issue or resume job" in help_text
    assert "enable              mark an issue ready for worker selection" in help_text
    assert "disable             remove worker queue labels from an issue" in help_text
    assert "reset               clear an issue run and mark it ready again" in help_text
    assert "clean               remove old run directories and worktrees" in help_text
    assert "retry" not in help_text


def test_cli_init_smoke(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["init", "--path", "config.yaml", "--no-create-labels"]) == 0
    assert (tmp_path / "config.yaml").exists()
    assert (tmp_path / ".vscode" / "settings.json").exists()
    tasks = (tmp_path / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    assert '"label": "Start a-dev"' in tasks
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".a-dev/" in gitignore


def test_cli_init_preserves_existing_vscode_files_unless_forced(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    vscode_dir = tmp_path / ".vscode"
    vscode_dir.mkdir()
    tasks = vscode_dir / "tasks.json"
    tasks.write_text('{"custom": true}\n', encoding="utf-8")

    assert cli.main(["init", "--path", "config.yaml", "--no-create-labels"]) == 0
    assert tasks.read_text(encoding="utf-8") == '{"custom": true}\n'
    assert (vscode_dir / "settings.json").exists()

    assert (
        cli.main(["init", "--path", "config.yaml", "--force", "--no-create-labels"])
        == 0
    )
    assert '"label": "Start a-dev"' in tasks.read_text(encoding="utf-8")


def test_cli_init_appends_missing_gitignore_entries_once(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist\n.a-dev\n", encoding="utf-8")

    assert cli.main(["init", "--path", "config.yaml", "--no-create-labels"]) == 0
    assert (
        cli.main(["init", "--path", "config.yaml", "--force", "--no-create-labels"])
        == 0
    )

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert lines.count(".a-dev") == 1
    assert lines.count(".a-dev/") == 0


def test_cli_init_infers_repo_and_branch_and_creates_labels(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    path = Path("config.yaml")
    captured = {}

    def fake_run_cmd(args):
        if args == ["git", "remote", "get-url", "origin"]:
            return CommandResult(
                "git remote get-url origin",
                0,
                "git@github.com:acme/widget.git\n",
                "",
                0.0,
            )
        if args == ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]:
            return CommandResult(
                "git symbolic-ref --short refs/remotes/origin/HEAD",
                0,
                "origin/trunk\n",
                "",
                0.0,
            )
        raise AssertionError(f"unexpected command: {args}")

    class FakeInitGH:
        def __init__(self, repo: str):
            captured["repo"] = repo

        def ensure_labels(self, labels):
            captured["labels"] = labels

    monkeypatch.setattr(cli, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(cli, "GHClient", FakeInitGH)

    assert cli.main(["init", "--path", str(path)]) == 0

    config = load_config(tmp_path / path)
    assert config.repo == "acme/widget"
    assert config.base_branch == "trunk"
    assert captured["repo"] == "acme/widget"
    assert set(captured["labels"]) >= {
        "ai-ready",
        "ai-resume",
        "ai-working",
        "ai-failed",
        "ai-pr-opened",
        "ai-parent",
        "ai-child",
        "ai-parent-done",
        "blocked",
        "needs-human",
    }


def test_repo_from_remote_url_supports_github_and_enterprise_remotes():
    assert (
        cli._repo_from_remote_url("https://github.com/owner/repo.git") == "owner/repo"
    )
    assert cli._repo_from_remote_url("git@github.com:owner/repo.git") == "owner/repo"
    assert (
        cli._repo_from_remote_url("ssh://git@github.example.com/owner/repo.git")
        == "github.example.com/owner/repo"
    )


def test_cli_list_smoke_with_fake_gh(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.setattr(cli, "GHClient", FakeGH)
    assert cli.main(["list", "--config", str(path)]) == 0
    assert "#1" in capsys.readouterr().out


def test_cli_list_defaults_to_dotfile_config(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / ".a-dev.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeGH)
    assert cli.main(["list"]) == 0
    assert "#1" in capsys.readouterr().out


def test_cli_list_all_issues_skips_candidate_filter(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeAllIssuesGH:
        def __init__(self, repo: str):
            self.repo = repo

        def list_issues(self, labels=None, state: str = "open"):
            captured["labels"] = labels
            captured["state"] = state
            return [
                Issue(1, "Ready", "", ["ai-ready"], "open"),
                Issue(2, "Done", "", [], "closed"),
            ]

        def blocked_by(self, number: int):
            raise AssertionError("all issue listing should not check blockers")

    monkeypatch.setattr(cli, "GHClient", FakeAllIssuesGH)

    assert cli.main(["list", "--config", str(path), "--all-issues"]) == 0

    output = capsys.readouterr().out
    assert captured == {"labels": None, "state": "all"}
    assert "#1" in output
    assert "#2" in output


def test_cli_list_open_issues_skips_candidate_filter(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeOpenIssuesGH:
        def __init__(self, repo: str):
            self.repo = repo

        def list_issues(self, labels=None, state: str = "open"):
            captured["labels"] = labels
            captured["state"] = state
            return [
                Issue(1, "Ready", "", ["ai-ready"], "open"),
                Issue(2, "Unlabeled", "", [], "open"),
            ]

        def blocked_by(self, number: int):
            raise AssertionError("open issue listing should not check blockers")

    monkeypatch.setattr(cli, "GHClient", FakeOpenIssuesGH)

    assert cli.main(["list", "--config", str(path), "--open-issues"]) == 0

    output = capsys.readouterr().out
    assert captured == {"labels": None, "state": "open"}
    assert "#1" in output
    assert "#2" in output


def test_cli_list_prs(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakePRGH:
        def __init__(self, repo: str):
            self.repo = repo

        def list_prs(self, state: str = "open"):
            captured["state"] = state
            return [
                PullRequest(
                    5,
                    "Worker PR",
                    "OPEN",
                    updated_at="2026-01-02T00:00:00Z",
                    labels=["ai-pr-opened"],
                    is_draft=True,
                    head_ref="ai/issue-5",
                    base_ref="main",
                )
            ]

    monkeypatch.setattr(cli, "GHClient", FakePRGH)

    assert cli.main(["list", "--config", str(path), "--prs"]) == 0

    output = capsys.readouterr().out
    assert captured == {"state": "all"}
    assert "#5\tOPEN\tdraft\t2026-01-02T00:00:00Z\tmain<-ai/issue-5" in output


def test_cli_list_open_prs(tmp_path: Path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeOpenPRGH:
        def __init__(self, repo: str):
            self.repo = repo

        def list_prs(self, state: str = "open"):
            captured["state"] = state
            return [
                PullRequest(
                    6,
                    "Open Worker PR",
                    "OPEN",
                    updated_at="2026-01-03T00:00:00Z",
                    is_draft=False,
                    head_ref="ai/issue-6",
                    base_ref="main",
                )
            ]

    monkeypatch.setattr(cli, "GHClient", FakeOpenPRGH)

    assert cli.main(["list", "--config", str(path), "--open-prs"]) == 0

    output = capsys.readouterr().out
    assert captured == {"state": "open"}
    assert "#6\tOPEN\tready\t2026-01-03T00:00:00Z\tmain<-ai/issue-6" in output


def test_run_once_passes_model_and_reasoning_overrides(tmp_path: Path, monkeypatch):
    path = tmp_path / ".a-dev.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    def fake_run_once(config_path, repo_root=None, overrides=None):
        assert overrides is not None
        captured["config_path"] = config_path
        captured["repo_root"] = repo_root
        captured["model"] = overrides.model
        captured["reasoning"] = overrides.reasoning
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "run_once", fake_run_once)
    assert cli.main(["run-once", "--model", "gpt-5.4", "--reasoning", "xhigh"]) == 0
    assert captured["config_path"] == Path(".a-dev.yaml")
    assert captured["model"] == "gpt-5.4"
    assert captured["reasoning"] == "xhigh"


def test_resume_passes_comment_and_overrides(tmp_path: Path, monkeypatch):
    path = tmp_path / ".a-dev.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    def fake_resume_issue(
        config_path, issue_number, manual_note="", repo_root=None, overrides=None
    ):
        assert overrides is not None
        captured["config_path"] = config_path
        captured["issue_number"] = issue_number
        captured["manual_note"] = manual_note
        captured["model"] = overrides.model
        captured["reasoning"] = overrides.reasoning
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "resume_issue", fake_resume_issue)

    assert (
        cli.main(
            [
                "resume",
                "123",
                "--comment",
                "Address the reviewer notes",
                "--model",
                "gpt-5.4-mini",
                "--reasoning",
                "high",
            ]
        )
        == 0
    )
    assert captured["config_path"] == Path(".a-dev.yaml")
    assert captured["issue_number"] == 123
    assert captured["manual_note"] == "Address the reviewer notes"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["reasoning"] == "high"


def test_resume_queue_adds_resume_label_and_comment(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / ".a-dev.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeResumeQueueGH:
        def __init__(self, repo: str):
            self.repo = repo

        def comment(self, number: int, body_file: Path):
            captured["issue"] = number
            captured["comment"] = body_file.read_text(encoding="utf-8")

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

        def add_label(self, number: int, label: str):
            captured.setdefault("added", []).append((number, label))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeResumeQueueGH)

    assert (
        cli.main(
            [
                "resume",
                "123",
                "--queue",
                "--comment",
                "Please address the latest review.",
            ]
        )
        == 0
    )
    assert captured["issue"] == 123
    assert captured["comment"] == "Please address the latest review.\n"
    assert captured["added"] == [(123, "ai-resume")]
    assert "issue #123 queued for resume" in capsys.readouterr().out


def test_enable_marks_failed_issue_ready_without_clearing_blockers(
    tmp_path: Path, monkeypatch, capsys
):
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeEnableGH:
        def __init__(self, repo: str):
            self.repo = repo

        def view_issue(self, number: int):
            captured["viewed"] = number
            return Issue(
                number,
                "Failed",
                "",
                ["ai-failed", "blocked", "needs-human"],
                "open",
            )

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

        def add_label(self, number: int, label: str):
            captured.setdefault("added", []).append((number, label))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeEnableGH)

    assert cli.main(["enable", "123"]) == 0

    assert captured["viewed"] == 123
    assert captured["removed"] == [(123, "ai-failed")]
    assert captured["added"] == [(123, "ai-ready")]
    assert "issue #123 enabled" in capsys.readouterr().out


def test_enable_run_now_processes_one_issue_with_overrides(tmp_path: Path, monkeypatch):
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeEnableGH:
        def __init__(self, repo: str):
            self.repo = repo

        def view_issue(self, number: int):
            return Issue(number, "Failed", "", ["ai-failed"], "open")

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

        def add_label(self, number: int, label: str):
            captured.setdefault("added", []).append((number, label))

    def fake_run_once(config_path, repo_root=None, overrides=None):
        assert overrides is not None
        captured["config_path"] = config_path
        captured["repo_root"] = repo_root
        captured["model"] = overrides.model
        captured["reasoning"] = overrides.reasoning
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeEnableGH)
    monkeypatch.setattr(cli, "run_once", fake_run_once)

    assert (
        cli.main(
            [
                "enable",
                "123",
                "--run-now",
                "--model",
                "gpt-5.4-mini",
                "--reasoning",
                "high",
            ]
        )
        == 0
    )

    assert captured["removed"] == [(123, "ai-failed")]
    assert captured["added"] == [(123, "ai-ready")]
    assert captured["config_path"] == Path(".a-dev.yaml")
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["reasoning"] == "high"


def test_disable_removes_ready_and_resume_labels(tmp_path: Path, monkeypatch, capsys):
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeDisableGH:
        def __init__(self, repo: str):
            self.repo = repo

        def view_issue(self, number: int):
            captured["viewed"] = number
            return Issue(
                number,
                "Queued",
                "",
                ["ai-ready", "ai-pr-opened", "ai-resume"],
                "open",
            )

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeDisableGH)

    assert cli.main(["disable", "123"]) == 0

    assert captured["viewed"] == 123
    assert captured["removed"] == [(123, "ai-ready"), (123, "ai-resume")]
    assert "issue #123 disabled" in capsys.readouterr().out


def test_reset_closes_pr_deletes_state_and_marks_issue_ready(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-123"
    run_dir.mkdir(parents=True)
    worktree = tmp_path / ".a-dev" / "worktrees" / "issue-123"
    worktree.mkdir(parents=True)
    (run_dir / "run-20260423-000000.json").write_text(
        json.dumps(
            {
                "issue_number": 123,
                "issue_title": "Fix bug",
                "branch_name": "ai/issue-123-fix-bug",
                "worktree_path": str(worktree),
                "status": "pr_opened",
                "phase": "finalizing",
                "started_at": "2026-04-23T00:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": "2026-04-23T00:10:00Z",
                "pr_url": "https://github.com/owner/repo/pull/9",
                "error_summary": None,
                "changed_files": [],
                "verifier_passed": True,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "latest.json").write_text(
        (run_dir / "run-20260423-000000.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    captured = {}

    class ResetGH:
        def __init__(self, repo: str):
            self.repo = repo

        def view_issue(self, number: int):
            captured["viewed"] = number
            return Issue(
                number,
                "Fix bug",
                "",
                ["ai-working", "ai-failed", "ai-pr-opened", "ai-resume"],
                "open",
            )

        def close_pr(self, pr_url: str, delete_branch: bool = False):
            captured["closed"] = (pr_url, delete_branch)

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed_labels", []).append((number, label))

        def add_label(self, number: int, label: str):
            captured.setdefault("added_labels", []).append((number, label))

    def fake_remove_worktree(path: Path, force: bool = False):
        captured["removed_worktree"] = (path, force)
        shutil.rmtree(path)

    def fake_delete_local_branch(branch: str, force: bool = False):
        captured["local_branch"] = (branch, force)

    monkeypatch.setattr(cli, "GHClient", ResetGH)
    monkeypatch.setattr(cli, "remove_worktree", fake_remove_worktree)
    monkeypatch.setattr(cli, "current_branch", lambda path=None: "main")
    monkeypatch.setattr(
        cli,
        "delete_remote_branch",
        lambda branch: captured.setdefault("remote_branch", branch),
    )
    monkeypatch.setattr(cli, "local_branch_exists", lambda branch: True)
    monkeypatch.setattr(cli, "delete_local_branch", fake_delete_local_branch)

    assert cli.main(["reset", "123"]) == 0

    assert captured["viewed"] == 123
    assert captured["closed"] == ("https://github.com/owner/repo/pull/9", True)
    assert captured["removed_labels"] == [
        (123, "ai-working"),
        (123, "ai-failed"),
        (123, "ai-pr-opened"),
        (123, "ai-resume"),
    ]
    assert captured["added_labels"] == [(123, "ai-ready")]
    assert captured["removed_worktree"] == (worktree, True)
    assert captured["remote_branch"] == "ai/issue-123-fix-bug"
    assert captured["local_branch"] == ("ai/issue-123-fix-bug", True)
    assert not run_dir.exists()
    assert "issue #123 reset" in capsys.readouterr().out


def test_status_reports_active_job_phase_and_diagnostic(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-87"
    run_dir.mkdir(parents=True)
    (run_dir / "codex-20260517-182637.log").write_text("done\n", encoding="utf-8")
    (run_dir / "codex.log").write_text("done\n", encoding="utf-8")
    (run_dir / "latest.json").write_text(
        json.dumps(
            {
                "issue_number": 87,
                "issue_title": "Add a scripts guide",
                "branch_name": "ai/issue-87-add-a-scripts-guide",
                "worktree_path": str(tmp_path / ".a-dev" / "worktrees" / "issue-87"),
                "status": "working",
                "phase": "codex_finished",
                "started_at": "2026-05-17T18:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": None,
                "pr_url": None,
                "error_summary": None,
                "changed_files": [],
                "verifier_passed": None,
            }
        ),
        encoding="utf-8",
    )

    class StatusGH:
        def __init__(self, repo: str):
            self.repo = repo

        def list_issues(self, labels):
            return [Issue(87, "Add a scripts guide", "", ["ai-working"], "open")]

    monkeypatch.setattr(cli, "GHClient", StatusGH)

    assert cli.main(["status"]) == 0

    output = capsys.readouterr().out
    assert "daemon: no" in output
    assert "run_once_lock: free" in output
    assert "active_or_stale_job:" in output
    assert "issue: #87 Add a scripts guide" in output
    assert "phase: codex_finished" in output
    assert "latest_artifact: codex-20260517-182637.log" in output
    assert "next_expected: verify.log" in output
    assert "Codex completed, verification has not started" in output
    assert "#87: Add a scripts guide" in output


def test_merge_uses_recorded_pr_and_deletes_branch(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-123"
    run_dir.mkdir(parents=True)
    (run_dir / "latest.json").write_text(
        json.dumps(
            {
                "issue_number": 123,
                "issue_title": "Fix bug",
                "branch_name": "ai/issue-123-fix-bug",
                "worktree_path": str(tmp_path / ".a-dev" / "worktrees" / "issue-123"),
                "status": "pr_opened",
                "phase": "finalizing",
                "started_at": "2026-04-23T00:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": "2026-04-23T00:05:00Z",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "error_summary": None,
                "changed_files": ["src/app.py"],
                "verifier_passed": True,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeMergeGH:
        def __init__(self, repo: str):
            captured["repo"] = repo

        def merge_pr(
            self,
            pr_url: str,
            method: str = "merge",
            *,
            auto: bool = False,
            admin: bool = False,
        ):
            captured["pr_url"] = pr_url
            captured["method"] = method
            captured["auto"] = auto
            captured["admin"] = admin

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

    monkeypatch.setattr(cli, "GHClient", FakeMergeGH)
    monkeypatch.setattr(cli, "_prepare_branch_for_merge", lambda job, repo_root: None)
    monkeypatch.setattr(
        cli,
        "_cleanup_merged_branch",
        lambda job, base_branch, repo_root: captured.setdefault(
            "cleanup", (job.branch_name, base_branch)
        ),
    )
    monkeypatch.setattr(
        cli,
        "_pull_base_after_merge",
        lambda base_branch, repo_root: (
            captured.setdefault("pulled", base_branch) or True
        ),
    )

    assert cli.main(["merge", "123", "--method", "squash"]) == 0

    latest = json.loads((run_dir / "latest.json").read_text(encoding="utf-8"))
    assert captured["repo"] == "owner/repo"
    assert captured["pr_url"] == "https://github.com/owner/repo/pull/5"
    assert captured["method"] == "squash"
    assert captured["auto"] is False
    assert captured["admin"] is False
    assert captured["cleanup"] == ("ai/issue-123-fix-bug", "main")
    assert captured["pulled"] == "main"
    assert captured["removed"] == [(123, "ai-pr-opened"), (123, "ai-resume")]
    assert latest["status"] == "pr_merged"
    output = capsys.readouterr().out
    assert "merged PR for issue #123" in output
    assert "pulled main" in output


def test_merge_auto_enables_auto_merge_without_deleting_branch(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-123"
    run_dir.mkdir(parents=True)
    (run_dir / "latest.json").write_text(
        json.dumps(
            {
                "issue_number": 123,
                "issue_title": "Fix bug",
                "branch_name": "ai/issue-123-fix-bug",
                "worktree_path": str(tmp_path / ".a-dev" / "worktrees" / "issue-123"),
                "status": "pr_opened",
                "phase": "finalizing",
                "started_at": "2026-04-23T00:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": "2026-04-23T00:05:00Z",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "error_summary": None,
                "changed_files": ["src/app.py"],
                "verifier_passed": True,
            }
        ),
        encoding="utf-8",
    )
    captured = {}

    class FakeAutoMergeGH:
        def __init__(self, repo: str):
            captured["repo"] = repo

        def merge_pr(
            self,
            pr_url: str,
            method: str = "merge",
            *,
            auto: bool = False,
            admin: bool = False,
        ):
            captured["pr_url"] = pr_url
            captured["method"] = method
            captured["auto"] = auto
            captured["admin"] = admin

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

    monkeypatch.setattr(cli, "GHClient", FakeAutoMergeGH)
    monkeypatch.setattr(cli, "_prepare_branch_for_merge", lambda job, repo_root: None)
    monkeypatch.setattr(
        cli,
        "_cleanup_merged_branch",
        lambda job, base_branch, repo_root: captured.setdefault("cleanup", True),
    )

    assert cli.main(["merge", "123", "--auto"]) == 0

    latest = json.loads((run_dir / "latest.json").read_text(encoding="utf-8"))
    assert captured["pr_url"] == "https://github.com/owner/repo/pull/5"
    assert captured["method"] == "merge"
    assert captured["auto"] is True
    assert captured["admin"] is False
    assert "cleanup" not in captured
    assert "removed" not in captured
    assert latest["status"] == "pr_auto_merge_enabled"
    output = capsys.readouterr().out
    assert "enabled auto-merge for issue #123" in output
    assert "kept branch ai/issue-123-fix-bug" in output


def test_merge_ready_marks_pr_ready_before_merging(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-123"
    run_dir.mkdir(parents=True)
    (run_dir / "latest.json").write_text(
        json.dumps(
            {
                "issue_number": 123,
                "issue_title": "Fix bug",
                "branch_name": "ai/issue-123-fix-bug",
                "worktree_path": str(tmp_path / ".a-dev" / "worktrees" / "issue-123"),
                "status": "pr_opened",
                "phase": "finalizing",
                "started_at": "2026-04-23T00:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": "2026-04-23T00:05:00Z",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "error_summary": None,
                "changed_files": ["src/app.py"],
                "verifier_passed": True,
            }
        ),
        encoding="utf-8",
    )
    captured: dict[str, Any] = {"calls": []}

    class FakeReadyMergeGH:
        def __init__(self, repo: str):
            captured["repo"] = repo

        def ready_pr(self, pr_url: str):
            captured["calls"].append(("ready", pr_url))

        def merge_pr(
            self,
            pr_url: str,
            method: str = "merge",
            *,
            auto: bool = False,
            admin: bool = False,
        ):
            captured["calls"].append(("merge", pr_url, method, auto, admin))

        def remove_label(self, number: int, label: str):
            captured.setdefault("removed", []).append((number, label))

    monkeypatch.setattr(cli, "GHClient", FakeReadyMergeGH)
    monkeypatch.setattr(cli, "_prepare_branch_for_merge", lambda job, repo_root: None)
    monkeypatch.setattr(
        cli,
        "_cleanup_merged_branch",
        lambda job, base_branch, repo_root: captured.setdefault(
            "cleanup", (job.branch_name, base_branch)
        ),
    )
    monkeypatch.setattr(
        cli, "_pull_base_after_merge", lambda base_branch, repo_root: True
    )

    assert cli.main(["merge", "123", "--ready", "--admin"]) == 0

    assert captured["calls"] == [
        ("ready", "https://github.com/owner/repo/pull/5"),
        ("merge", "https://github.com/owner/repo/pull/5", "merge", False, True),
    ]
    assert "merged PR for issue #123" in capsys.readouterr().out


def test_merge_rejects_latest_non_pr_status(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_dir = tmp_path / ".a-dev" / "runs" / "issue-123"
    run_dir.mkdir(parents=True)
    (run_dir / "latest.json").write_text(
        json.dumps(
            {
                "issue_number": 123,
                "issue_title": "Fix bug",
                "branch_name": "ai/issue-123-fix-bug",
                "worktree_path": str(tmp_path / ".a-dev" / "worktrees" / "issue-123"),
                "status": "verify_failed",
                "phase": "finalizing",
                "started_at": "2026-04-23T00:00:00Z",
                "base_branch": "main",
                "stack_depth": 0,
                "blocker_issue_numbers": [],
                "finished_at": "2026-04-23T00:05:00Z",
                "pr_url": "https://github.com/owner/repo/pull/5",
                "error_summary": "pytest failed",
                "changed_files": ["src/app.py"],
                "verifier_passed": False,
            }
        ),
        encoding="utf-8",
    )

    assert cli.main(["merge", "123"]) == 1
    assert "not an active A-Dev PR" in capsys.readouterr().err


def test_cli_create_issue_uses_ready_label_and_generated_body(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["repo"] = self.repo
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/123"

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir, mode="auto": (
            cli.IssueDraftPlan(
                "single",
                issue=cli.IssueDraft(
                    "Generated parser failure title",
                    "## Summary\n\nGenerated body.\n",
                ),
            )
        ),
    )
    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    result = cli.main(
        [
            "create",
            "--config",
            str(path),
            "--mode",
            "single",
            "--no-edit",
            "Parser fails on empty input",
        ]
    )

    assert result == 0
    assert captured["repo"] == "owner/repo"
    assert captured["title"] == "Generated parser failure title"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nGenerated body.\n"
    assert (
        "created issue: https://github.com/owner/repo/issues/123"
        in capsys.readouterr().out
    )


def test_cli_create_issue_derives_title_and_runs_editor(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/124"

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir, mode="auto": (
            cli.IssueDraftPlan(
                "single",
                issue=cli.IssueDraft(
                    "Initial generated title",
                    "## Summary\n\nInitial generated body.\n",
                ),
            )
        ),
    )

    def fake_editor(body_file: Path, editor: str | None = None):
        body_file.write_text(
            "Title: Edited title\n\n## Summary\n\nEdited in editor.\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)
    monkeypatch.setattr(cli, "_run_editor", fake_editor)

    result = cli.main(
        ["create", "--config", str(path), "Fix parser crash", "when input is empty"]
    )

    assert result == 0
    assert captured["title"] == "Edited title"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nEdited in editor.\n"


def test_cli_create_parses_title_line_from_description_file(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    description_file = tmp_path / "issue.md"
    description_file.write_text(
        "Title: Fix parser crash\n\nParser crashes when input is empty.\n",
        encoding="utf-8",
    )
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/125"

    def fake_generate_issue_draft(
        config, repo_root, description, title_hint, draft_dir, mode="auto"
    ):
        captured["description"] = description
        captured["title_hint"] = title_hint
        return cli.IssueDraftPlan(
            "single",
            issue=cli.IssueDraft(title_hint, "## Summary\n\nGenerated body.\n"),
        )

    monkeypatch.setattr(cli, "_generate_issue_draft", fake_generate_issue_draft)
    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)

    result = cli.main(
        [
            "create",
            "--config",
            str(path),
            "--description-file",
            str(description_file),
            "--mode",
            "single",
            "--no-edit",
        ]
    )

    assert result == 0
    assert captured["description"] == "Parser crashes when input is empty."
    assert captured["title_hint"] == "Fix parser crash"
    assert captured["title"] == "Fix parser crash"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nGenerated body.\n"


def test_cli_create_input_editor_reads_temporary_notes(tmp_path: Path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeCreateGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue(self, title: str, body_file: Path, labels: list[str]):
            captured["title"] = title
            captured["body"] = body_file.read_text(encoding="utf-8")
            captured["labels"] = labels
            return "https://github.com/owner/repo/issues/126"

    def fake_editor(draft_file: Path, editor: str | None = None):
        captured["input_template"] = draft_file.read_text(encoding="utf-8")
        draft_file.write_text(
            "Title: Fix task issue input\n\nUse a temp editor input file.\n",
            encoding="utf-8",
        )

    def fake_generate_issue_draft(
        config, repo_root, description, title_hint, draft_dir, mode="auto"
    ):
        captured["description"] = description
        captured["title_hint"] = title_hint
        return cli.IssueDraftPlan(
            "single",
            issue=cli.IssueDraft(title_hint, "## Summary\n\nGenerated body.\n"),
        )

    monkeypatch.setattr(cli, "_run_editor", fake_editor)
    monkeypatch.setattr(cli, "_generate_issue_draft", fake_generate_issue_draft)
    monkeypatch.setattr(cli, "GHClient", FakeCreateGH)

    result = cli.main(
        [
            "create",
            "--config",
            str(path),
            "--input-editor",
            "--mode",
            "single",
            "--no-edit",
        ]
    )

    assert result == 0
    assert captured["input_template"] == "Title:\n\n"
    assert captured["description"] == "Use a temp editor input file."
    assert captured["title_hint"] == "Fix task issue input"
    assert captured["title"] == "Fix task issue input"
    assert captured["labels"] == ["ai-ready"]
    assert captured["body"] == "## Summary\n\nGenerated body.\n"


def test_cli_create_parent_issue_links_children_and_dependencies(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {"created": [], "sub_issues": [], "dependencies": []}

    class FakeCreateParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def create_issue_record(self, title: str, body_file: Path, labels: list[str]):
            number = len(captured["created"]) + 100
            issue = CreatedIssue(
                number=number,
                title=title,
                url=f"https://github.com/owner/repo/issues/{number}",
                id=number + 1000,
            )
            captured["created"].append(
                {
                    "title": title,
                    "body": body_file.read_text(encoding="utf-8"),
                    "labels": labels,
                    "issue": issue,
                }
            )
            return issue

        def add_sub_issue(self, parent_number: int, child_issue_id: int):
            captured["sub_issues"].append((parent_number, child_issue_id))

        def add_blocked_by(self, issue_number: int, blocking_issue_id: int):
            captured["dependencies"].append((issue_number, blocking_issue_id))

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir, mode="auto": (
            cli.IssueDraftPlan(
                "parent",
                parent=cli.IssueDraft("Parent title", "## Summary\n\nParent body."),
                children=[
                    cli.ChildIssueDraft(
                        "API child", "## Summary\n\nAPI body.", "api", []
                    ),
                    cli.ChildIssueDraft(
                        "Impl child", "## Summary\n\nImpl body.", "impl", ["api"]
                    ),
                ],
            )
        ),
    )
    monkeypatch.setattr(cli, "GHClient", FakeCreateParentGH)

    result = cli.main(
        [
            "create",
            "--config",
            str(path),
            "--mode",
            "parent",
            "--no-edit",
            "Large refactor",
        ]
    )

    assert result == 0
    assert [item["title"] for item in captured["created"]] == [
        "Parent title",
        "API child",
        "Impl child",
    ]
    assert captured["created"][0]["labels"] == ["ai-ready", "ai-parent"]
    assert captured["created"][1]["labels"] == ["ai-child"]
    assert captured["created"][2]["labels"] == ["ai-child"]
    assert captured["sub_issues"] == [(100, 1101), (100, 1102)]
    assert captured["dependencies"] == [(102, 1101)]
    assert (
        "created issue: https://github.com/owner/repo/issues/100"
        in capsys.readouterr().out
    )


def test_cli_create_rejects_invalid_parent_plan_before_github(
    tmp_path: Path, monkeypatch
):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")

    class UnexpectedGH:
        def __init__(self, repo: str):
            raise AssertionError("GitHub should not be called")

    monkeypatch.setattr(
        cli,
        "_generate_issue_draft",
        lambda config, repo_root, description, title_hint, draft_dir, mode="auto": (
            cli._parse_issue_draft_json(
                '{"kind":"parent","parent":{"title":"Parent","body":"Body"},"children":[{"key":"a","title":"A","body":"Body","blocked_by":["missing"]}]}',
                requested_mode=mode,
            )
        ),
    )
    monkeypatch.setattr(cli, "GHClient", UnexpectedGH)

    assert (
        cli.main(
            [
                "create",
                "--config",
                str(path),
                "--mode",
                "parent",
                "--no-edit",
                "Bad graph",
            ]
        )
        == 1
    )


def test_cli_kanban_writes_obsidian_markdown_from_run_artifacts(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".a-dev.yaml").write_text("repo: owner/repo\n", encoding="utf-8")
    run_root = tmp_path / ".a-dev" / "runs"

    success_dir = run_root / "issue-42"
    success_dir.mkdir(parents=True)
    success_record = {
        "issue_number": 42,
        "issue_title": "Add kanban",
        "branch_name": "ai/issue-42-add-kanban",
        "worktree_path": str(tmp_path / "worktrees" / "issue-42"),
        "status": "pr_opened",
        "phase": "finalizing",
        "started_at": "2026-04-23T00:00:00Z",
        "base_branch": "main",
        "stack_depth": 0,
        "blocker_issue_numbers": [],
        "finished_at": "2026-04-23T00:05:00Z",
        "pr_url": "https://github.com/owner/repo/pull/99",
        "error_summary": None,
        "changed_files": ["src/ai_issue_worker/cli.py"],
        "verifier_passed": True,
    }
    (success_dir / "latest.json").write_text(
        json.dumps(success_record), encoding="utf-8"
    )
    (success_dir / "summary.md").write_text(
        "## What changed\n"
        "- Added kanban markdown generation.\n"
        "- Aggregated latest run summaries instead of review rounds.\n"
        "\n"
        "## Follow-up context\n"
        "- No follow-up needed.\n",
        encoding="utf-8",
    )
    (success_dir / "codex-20260423.log").write_text(
        "input tokens: 100\noutput tokens: 20\ntotal tokens: 120\n",
        encoding="utf-8",
    )

    failed_dir = run_root / "issue-43"
    failed_dir.mkdir(parents=True)
    failed_record = {
        "issue_number": 43,
        "issue_title": "Fail verifier",
        "branch_name": "ai/issue-43-fail-verifier",
        "worktree_path": str(tmp_path / "worktrees" / "issue-43"),
        "status": "verify_failed",
        "phase": "finalizing",
        "started_at": "2026-04-22T00:00:00Z",
        "base_branch": "main",
        "stack_depth": 0,
        "blocker_issue_numbers": [],
        "finished_at": "2026-04-22T00:05:00Z",
        "pr_url": None,
        "error_summary": "pytest failed in test_cli.py",
        "changed_files": ["tests/test_cli.py"],
        "verifier_passed": False,
    }
    (failed_dir / "latest.json").write_text(json.dumps(failed_record), encoding="utf-8")

    assert cli.main(["kanban"]) == 0

    output = (tmp_path / ".a-dev" / "kanban.md").read_text(encoding="utf-8")
    assert output.startswith("## repo\n")
    assert "- [x] **Issue 42**: <br>- Added kanban markdown generation.;" in output
    assert (
        "Token usage: input=100 output=20 total=120 across 1/1 Codex run(s)" in output
    )
    assert (
        "PR status: opened or updated at https://github.com/owner/repo/pull/99"
        in output
    )
    assert "- [ ] **Issue 43**: <br>- Failure: pytest failed in test_cli.py;" in output
    assert "PR status: not opened; latest run status verify_failed" in output
    captured = capsys.readouterr()
    assert captured.out == output
    assert "wrote " in captured.err


def test_parse_issue_draft_file_requires_title_line():
    try:
        cli._parse_issue_draft_file("## Summary\n\nBody")
    except cli.ConfigError as exc:
        assert "Title:" in str(exc)
    else:
        raise AssertionError("expected ConfigError")


def test_editor_command_defaults_to_code_wait(monkeypatch):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.delenv("EDITOR", raising=False)
    monkeypatch.setattr(
        cli.shutil,
        "which",
        lambda name: "/usr/local/bin/code" if name == "code" else None,
    )

    assert cli._editor_command() == ["/usr/local/bin/code", "--wait"]
