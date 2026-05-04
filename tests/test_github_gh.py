import json
from pathlib import Path

from ai_issue_worker.github_gh import GHClient


class Result:
    exit_code = 0
    stderr = ""
    command = "gh api"

    def __init__(self, stdout: str):
        self.stdout = stdout


def test_blocked_by_uses_gh_api_and_flattens_paginated_results(monkeypatch):
    captured = {}
    pages = [
        [
            {
                "number": 2,
                "title": "Open blocker",
                "body": "",
                "labels": [],
                "state": "open",
                "updated_at": "2026-01-01",
            }
        ],
        [
            {
                "number": 3,
                "title": "Closed blocker",
                "body": "",
                "labels": [],
                "state": "closed",
            }
        ],
    ]

    def fake_run_cmd(args):
        captured["args"] = args
        return Result(json.dumps(pages))

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    blockers = GHClient("owner/repo").blocked_by(1)

    assert captured["args"] == [
        "gh",
        "api",
        "repos/owner/repo/issues/1/dependencies/blocked_by",
        "--paginate",
        "--slurp",
    ]
    assert [issue.number for issue in blockers] == [2, 3]


def test_blocked_by_passes_hostname_for_hosted_repo(monkeypatch):
    captured = {}

    def fake_run_cmd(args):
        captured["args"] = args
        return Result("[]")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    GHClient("github.example.com/owner/repo").blocked_by(1)

    assert captured["args"][:5] == [
        "gh",
        "api",
        "--hostname",
        "github.example.com",
        "repos/owner/repo/issues/1/dependencies/blocked_by",
    ]


def test_create_issue_record_loads_rest_id(monkeypatch, tmp_path: Path):
    captured = []
    body = tmp_path / "body.md"
    body.write_text("Body\n", encoding="utf-8")

    def fake_run_cmd(args):
        captured.append(args)
        if args[:3] == ["gh", "issue", "create"]:
            return Result("https://github.com/owner/repo/issues/42\n")
        return Result(
            json.dumps(
                {
                    "id": 9001,
                    "number": 42,
                    "title": "Title",
                    "body": "Body",
                    "labels": [],
                    "state": "open",
                }
            )
        )

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    issue = GHClient("owner/repo").create_issue_record(
        "Title", body, labels=["ai-ready"]
    )

    assert issue.number == 42
    assert issue.id == 9001
    assert captured[1] == ["gh", "api", "repos/owner/repo/issues/42"]


def test_sub_issues_uses_gh_api_and_flattens_paginated_results(monkeypatch):
    captured = {}

    def fake_run_cmd(args):
        captured["args"] = args
        return Result(
            json.dumps(
                [
                    [
                        {
                            "id": 8,
                            "number": 2,
                            "title": "Child",
                            "body": "",
                            "labels": [],
                            "state": "open",
                        }
                    ]
                ]
            )
        )

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    children = GHClient("owner/repo").sub_issues(1)

    assert captured["args"] == [
        "gh",
        "api",
        "repos/owner/repo/issues/1/sub_issues",
        "--paginate",
        "--slurp",
    ]
    assert children[0].number == 2
    assert children[0].id == 8


def test_add_sub_issue_and_dependency_use_rest_ids(monkeypatch):
    captured = []

    def fake_run_cmd(args):
        captured.append(args)
        return Result("{}")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    gh = GHClient("github.example.com/owner/repo")
    gh.add_sub_issue(10, 1001)
    gh.add_blocked_by(11, 1002)

    assert captured == [
        [
            "gh",
            "api",
            "--hostname",
            "github.example.com",
            "-X",
            "POST",
            "repos/owner/repo/issues/10/sub_issues",
            "-f",
            "sub_issue_id=1001",
        ],
        [
            "gh",
            "api",
            "--hostname",
            "github.example.com",
            "-X",
            "POST",
            "repos/owner/repo/issues/11/dependencies/blocked_by",
            "-f",
            "issue_id=1002",
        ],
    ]


def test_ensure_labels_creates_or_updates_each_label(monkeypatch):
    captured = []

    def fake_run_cmd(args):
        captured.append(args)
        return Result("")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    GHClient("owner/repo").ensure_labels(
        {
            "ai-ready": ("0E8A16", "Ready for automation."),
            "ai-working": ("1D76DB", "Automation is working."),
        }
    )

    assert captured == [
        [
            "gh",
            "label",
            "create",
            "ai-ready",
            "--repo",
            "owner/repo",
            "--color",
            "0E8A16",
            "--description",
            "Ready for automation.",
            "--force",
        ],
        [
            "gh",
            "label",
            "create",
            "ai-working",
            "--repo",
            "owner/repo",
            "--color",
            "1D76DB",
            "--description",
            "Automation is working.",
            "--force",
        ],
    ]


def test_create_pr_sanitizes_body_file_before_pushing(monkeypatch, tmp_path: Path):
    captured = {}
    body = tmp_path / "body.md"
    body.write_text(
        "Verifier failed in /Users/alice/Repos/project/src/app.py\n", encoding="utf-8"
    )

    def fake_run_cmd(args):
        body_path = Path(args[args.index("--body-file") + 1])
        captured["body"] = body_path.read_text(encoding="utf-8")
        captured["args"] = args
        return Result("https://github.com/owner/repo/pull/1\n")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    url = GHClient("owner/repo").create_pr("main", "ai/issue-1", "Title", body)

    assert url == "https://github.com/owner/repo/pull/1"
    assert "/Users/alice" not in captured["body"]
    assert "####/Repos/project/src/app.py" in captured["body"]
    assert captured["args"][captured["args"].index("--body-file") + 1] != str(body)


def test_issue_comments_uses_issue_comments_api(monkeypatch):
    captured = {}

    def fake_run_cmd(args):
        captured["args"] = args
        return Result(
            json.dumps(
                [
                    [
                        {
                            "body": "Please fix",
                            "created_at": "2026-01-01T00:00:00Z",
                            "user": {"login": "alice"},
                        }
                    ]
                ]
            )
        )

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    comments = GHClient("owner/repo").issue_comments(7)

    assert captured["args"] == [
        "gh",
        "api",
        "repos/owner/repo/issues/7/comments",
        "--paginate",
        "--slurp",
    ]
    assert len(comments) == 1
    assert comments[0].source == "issue comment"
    assert comments[0].author == "alice"
    assert comments[0].body == "Please fix"


def test_pr_reviews_uses_reviews_api(monkeypatch):
    captured = {}

    def fake_run_cmd(args):
        captured["args"] = args
        return Result(
            json.dumps(
                [
                    [
                        {
                            "body": "Needs another test",
                            "submitted_at": "2026-01-02T00:00:00Z",
                            "user": {"login": "bob"},
                        }
                    ]
                ]
            )
        )

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    reviews = GHClient("owner/repo").pr_reviews("https://github.com/owner/repo/pull/17")

    assert captured["args"] == [
        "gh",
        "api",
        "repos/owner/repo/pulls/17/reviews",
        "--paginate",
        "--slurp",
    ]
    assert len(reviews) == 1
    assert reviews[0].source == "pull request review"
    assert reviews[0].author == "bob"
    assert reviews[0].body == "Needs another test"


def test_update_pr_sanitizes_body_file_before_edit(monkeypatch, tmp_path: Path):
    captured = {}
    body = tmp_path / "body.md"
    body.write_text(
        "Updated in /Users/alice/Repos/project/src/app.py\n", encoding="utf-8"
    )

    def fake_run_cmd(args):
        body_path = Path(args[args.index("--body-file") + 1])
        captured["body"] = body_path.read_text(encoding="utf-8")
        captured["args"] = args
        return Result("")

    monkeypatch.setattr("ai_issue_worker.github_gh.run_cmd", fake_run_cmd)

    GHClient("owner/repo").update_pr(
        "https://github.com/owner/repo/pull/2", "Updated Title", body
    )

    assert captured["args"][:6] == [
        "gh",
        "pr",
        "edit",
        "https://github.com/owner/repo/pull/2",
        "--repo",
        "owner/repo",
    ]
    assert "/Users/alice" not in captured["body"]
    assert "####/Repos/project/src/app.py" in captured["body"]
