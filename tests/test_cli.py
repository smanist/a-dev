from pathlib import Path

from ai_issue_worker import cli
from ai_issue_worker.config import load_config
from ai_issue_worker.models import CommandResult, CreatedIssue, Issue


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


def test_cli_init_smoke(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["init", "--path", "config.yaml", "--no-create-labels"]) == 0
    assert (tmp_path / "config.yaml").exists()
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".ai-logs" in gitignore
    assert ".ai-runs" in gitignore
    assert ".ai-runtime" in gitignore
    assert ".ai-worktrees" in gitignore


def test_cli_init_appends_missing_gitignore_entries_once(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("dist\n.ai-runs\n", encoding="utf-8")

    assert cli.main(["init", "--path", "config.yaml", "--no-create-labels"]) == 0
    assert (
        cli.main(["init", "--path", "config.yaml", "--force", "--no-create-labels"])
        == 0
    )

    lines = gitignore.read_text(encoding="utf-8").splitlines()
    assert lines.count(".ai-runs") == 1
    assert lines.count(".ai-logs") == 1
    assert lines.count(".ai-runtime") == 1
    assert lines.count(".ai-worktrees") == 1


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
    path = tmp_path / ".ai-issue-worker.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "GHClient", FakeGH)
    assert cli.main(["list"]) == 0
    assert "#1" in capsys.readouterr().out


def test_run_once_passes_model_and_reasoning_overrides(tmp_path: Path, monkeypatch):
    path = tmp_path / ".ai-issue-worker.yaml"
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
    assert captured["config_path"] == Path(".ai-issue-worker.yaml")
    assert captured["model"] == "gpt-5.4"
    assert captured["reasoning"] == "xhigh"


def test_resume_passes_comment_and_overrides(tmp_path: Path, monkeypatch):
    path = tmp_path / ".ai-issue-worker.yaml"
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
    assert captured["config_path"] == Path(".ai-issue-worker.yaml")
    assert captured["issue_number"] == 123
    assert captured["manual_note"] == "Address the reviewer notes"
    assert captured["model"] == "gpt-5.4-mini"
    assert captured["reasoning"] == "high"


def test_resume_queue_adds_resume_label_and_comment(
    tmp_path: Path, monkeypatch, capsys
):
    path = tmp_path / ".ai-issue-worker.yaml"
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


def test_parse_issue_draft_file_requires_title_line():
    try:
        cli._parse_issue_draft_file("## Summary\n\nBody")
    except cli.ConfigError as exc:
        assert "Title:" in str(exc)
    else:
        raise AssertionError("expected ConfigError")
