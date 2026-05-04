import subprocess
from pathlib import Path

from ai_issue_worker.config import config_from_dict
from ai_issue_worker.jobs import load_job_record, write_job_record
from ai_issue_worker.models import (
    AgentResult,
    CommandResult,
    DiffSummary,
    DiscussionComment,
    Issue,
    JobRecord,
    VerifyResult,
)
from ai_issue_worker.runner import (
    EXIT_OK,
    EXIT_VERIFY,
    IssueWorkPlan,
    _diff_snapshot,
    _run_agent_and_verify,
    blocking_review_priorities,
    process_issue,
    process_issue_resume,
    process_parent_issue,
    run_once,
    select_work_plan,
)


def _issue() -> Issue:
    return Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")


def _diff() -> DiffSummary:
    return DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)


class FakeDependencyGH:
    def __init__(self, blockers_by_issue: dict[int, list[Issue]]):
        self.blockers_by_issue = blockers_by_issue
        self.checked: list[int] = []

    def blocked_by(self, number: int) -> list[Issue]:
        self.checked.append(number)
        return self.blockers_by_issue.get(number, [])


def test_blocking_review_priorities_prefers_structured_line():
    review = "BLOCKING_PRIORITIES: P1\n\n[P1] Bad edge case\n[P2] Cleanup"

    assert blocking_review_priorities(review, ["P0", "P1"]) == ["P1"]


def test_blocking_review_priorities_fallback_ignores_plain_no_p0_p1_text():
    review = "No P0/P1 findings found.\n\n[P2] Optional cleanup"

    assert blocking_review_priorities(review, ["P0", "P1"]) == []


def test_select_workable_issue_skips_candidates_with_open_blockers():
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    paths = {"run_root": Path("/missing")}
    issues = [
        Issue(
            1, "Blocked", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z"
        ),
        Issue(2, "Ready", "", ["ai-ready"], "open", updated_at="2026-01-02T00:00:00Z"),
    ]
    gh = FakeDependencyGH({1: [Issue(10, "Blocker", "", [], "open")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 2
    assert plan.base_branch == "main"
    assert gh.checked == [1, 2]


def test_select_workable_issue_allows_candidates_with_closed_blockers():
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    paths = {"run_root": Path("/missing")}
    issues = [
        Issue(
            1, "Unblocked", "", ["ai-ready"], "open", updated_at="2026-01-01T00:00:00Z"
        )
    ]
    gh = FakeDependencyGH({1: [Issue(10, "Done blocker", "", [], "closed")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 1
    assert plan.base_branch == "main"


def test_select_workable_issue_can_ignore_dependency_checks():
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"respect_issue_dependencies": False}}
    ).issue_selection
    paths = {"run_root": Path("/missing")}
    issues = [
        Issue(
            1,
            "Blocked but selected",
            "",
            ["ai-ready"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH({1: [Issue(10, "Blocker", "", [], "open")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 1
    assert plan.base_branch == "main"
    assert gh.checked == []


def _write_pr_job(
    run_root: Path, issue_number: int, branch_name: str, stack_depth: int = 0
) -> None:
    write_job_record(
        run_root / f"issue-{issue_number}",
        JobRecord(
            issue_number=issue_number,
            issue_title=f"Issue {issue_number}",
            branch_name=branch_name,
            worktree_path=f"/tmp/issue-{issue_number}",
            status="pr_opened",
            started_at="2026-01-01T00:00:00Z",
            base_branch="main",
            stack_depth=stack_depth,
            pr_url=f"https://github.com/owner/repo/pull/{issue_number}",
        ),
        timestamp="20260101-000000",
    )


def test_select_workable_issue_stacks_on_blocker_pr_branch(tmp_path: Path):
    config = config_from_dict(
        {
            "repo": "owner/repo",
            "issue_selection": {"allow_stacked_prs": True, "max_stack_depth": 3},
        }
    ).issue_selection
    paths = {"run_root": tmp_path}
    _write_pr_job(tmp_path, 10, "ai/issue-10-base", stack_depth=0)
    issues = [
        Issue(
            11,
            "Downstream",
            "",
            ["ai-ready"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH({11: [Issue(10, "Blocker", "", [], "open")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 11
    assert plan.base_branch == "ai/issue-10-base"
    assert plan.stack_depth == 1
    assert plan.blocker_issue_numbers == [10]


def test_select_workable_issue_skips_stacking_without_blocker_pr(tmp_path: Path):
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"allow_stacked_prs": True}}
    ).issue_selection
    paths = {"run_root": tmp_path}
    issues = [
        Issue(
            11,
            "Downstream",
            "",
            ["ai-ready"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH({11: [Issue(10, "Blocker", "", [], "open")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan is None


def test_select_workable_issue_skips_multiple_open_blockers(tmp_path: Path):
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"allow_stacked_prs": True}}
    ).issue_selection
    paths = {"run_root": tmp_path}
    _write_pr_job(tmp_path, 10, "ai/issue-10-a")
    _write_pr_job(tmp_path, 20, "ai/issue-20-b")
    issues = [
        Issue(
            30,
            "Downstream",
            "",
            ["ai-ready"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH(
        {
            30: [
                Issue(10, "Blocker A", "", [], "open"),
                Issue(20, "Blocker B", "", [], "open"),
            ]
        }
    )

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan is None


def test_select_workable_issue_respects_max_stack_depth(tmp_path: Path):
    config = config_from_dict(
        {
            "repo": "owner/repo",
            "issue_selection": {"allow_stacked_prs": True, "max_stack_depth": 1},
        }
    ).issue_selection
    paths = {"run_root": tmp_path}
    _write_pr_job(tmp_path, 10, "ai/issue-10-base", stack_depth=1)
    issues = [
        Issue(
            11,
            "Downstream",
            "",
            ["ai-ready"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH({11: [Issue(10, "Blocker", "", [], "open")]})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan is None


def test_select_workable_issue_can_pick_queued_resume(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    paths = {"run_root": tmp_path}
    _write_pr_job(tmp_path, 12, "ai/issue-12-existing")
    issues = [
        Issue(
            12,
            "Resume",
            "",
            ["ai-pr-opened", "ai-resume"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        ),
        Issue(13, "Fresh", "", ["ai-ready"], "open", updated_at="2026-01-02T00:00:00Z"),
    ]
    gh = FakeDependencyGH({})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 12
    assert plan.mode == "resume"
    assert plan.resume_record is not None
    assert plan.resume_record.branch_name == "ai/issue-12-existing"


def test_select_workable_issue_marks_parent_mode():
    config = config_from_dict({"repo": "owner/repo"}).issue_selection
    paths = {"run_root": Path("/missing")}
    issues = [
        Issue(
            1,
            "Parent",
            "",
            ["ai-ready", "ai-parent"],
            "open",
            updated_at="2026-01-01T00:00:00Z",
        )
    ]
    gh = FakeDependencyGH({})

    plan = select_work_plan(gh, issues, config, "main", paths)

    assert plan and plan.issue.number == 1
    assert plan.mode == "parent"
    assert gh.checked == []


def test_run_agent_review_fix_loop_until_clean(monkeypatch, tmp_path: Path):
    config = config_from_dict(
        {"repo": "owner/repo", "verify": {"commands": ["pytest"]}}
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    calls: list[str] = []
    commands: list[str | None] = []
    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Broken edge case",
            "fixed blocking review findings",
            "BLOCKING_PRIORITIES: NONE\n\nNo findings.",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        calls.append(prompt_path.read_text(encoding="utf-8").splitlines()[0])
        commands.append(command)
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    verify_count = 0

    def fake_verifier(config, worktree_path, log_path):
        nonlocal verify_count
        verify_count += 1
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff"
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is True
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert error == ""
    assert failure_kind == ""
    assert calls == [
        "# Task",
        "# Code review task",
        "# Review fix task",
        "# Code review task",
    ]
    assert commands == [None, config.review.command, None, config.review.command]
    assert verify_count == 2


def test_run_agent_review_loop_stops_after_max_fix_iterations(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict({"repo": "owner/repo", "review": {"max_iterations": 1}})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Broken edge case",
            "fixed blocking review findings",
            "BLOCKING_PRIORITIES: P1\n\n[P1] Still broken",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff"
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is False
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert "1 fix iteration" in error
    assert "[P1] Still broken" in error
    assert failure_kind == "review"


def test_run_agent_fails_when_review_session_modifies_worktree(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict({"repo": "owner/repo"})
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outputs = iter(
        ["implementation complete", "BLOCKING_PRIORITIES: NONE\n\nNo findings."]
    )
    snapshots = iter(["before-review", "after-review"])

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: next(snapshots)
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )

    ok, verify, diff, error, failure_kind = _run_agent_and_verify(
        config,
        _issue(),
        tmp_path,
        tmp_path,
        run_dir,
        "20260424",
    )

    assert ok is False
    assert verify and verify.passed is True
    assert diff.changed_files == ["src/app.py"]
    assert "modified the worktree" in error


def test_process_issue_resume_reuses_existing_pr_and_includes_follow_up(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict(
        {"repo": "owner/repo", "verify": {"commands": ["pytest"]}}
    )
    run_root = tmp_path / "runs"
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    issue_run_dir = run_root / "issue-123"
    issue_run_dir.mkdir(parents=True)
    (issue_run_dir / "summary.md").write_text(
        "## What changed\n- Added nil handling.\n\n## Decisions to preserve\n- Keep the API stable.\n\n## Follow-up context\n- Watch edge cases.\n",
        encoding="utf-8",
    )
    previous = JobRecord(
        issue_number=123,
        issue_title="Bug title",
        branch_name="ai/issue-123-bug-title",
        worktree_path=str(worktree_path),
        status="pr_opened",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:10:00Z",
        base_branch="main",
        pr_url="https://github.com/owner/repo/pull/5",
    )
    issue = Issue(123, "Bug title", "Bug body", ["ai-pr-opened"], "open")
    prompts: list[str] = []
    updated_pr = {}
    ensured = {}

    class FakeResumeGH:
        def __init__(self, repo: str):
            self.repo = repo

        def issue_comments(self, number: int):
            assert number == 123
            return [
                DiscussionComment(
                    "issue comment",
                    "Draft PR opened: https://github.com/owner/repo/pull/5",
                    "worker",
                    "2026-01-02T00:00:00Z",
                ),
                DiscussionComment(
                    "issue comment",
                    "Please add a regression test.",
                    "alice",
                    "2026-01-02T00:01:00Z",
                ),
            ]

        def pr_comments(self, pr_url: str):
            assert pr_url == previous.pr_url
            return [
                DiscussionComment(
                    "pull request comment",
                    "Handle the nil case too.",
                    "bob",
                    "2026-01-02T00:02:00Z",
                )
            ]

        def pr_reviews(self, pr_url: str):
            assert pr_url == previous.pr_url
            return [
                DiscussionComment(
                    "pull request review", "Old review", "carol", "2026-01-01T00:05:00Z"
                ),
                DiscussionComment(
                    "pull request review",
                    "One more edge case looks untested.",
                    "dora",
                    "2026-01-02T00:03:00Z",
                ),
            ]

        def add_label(self, number: int, label: str) -> None:
            pass

        def remove_label(self, number: int, label: str) -> None:
            pass

        def comment(self, number: int, body_file: Path) -> None:
            pass

        def update_pr(self, pr_url: str, title: str, body_file: Path) -> None:
            updated_pr["url"] = pr_url
            updated_pr["title"] = title
            updated_pr["body"] = body_file.read_text(encoding="utf-8")

    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: NONE\n\nNo findings.",
            "## What changed\n- Added regression coverage.\n\n## Decisions to preserve\n- Keep the API stable.\n\n## Follow-up context\n- Review any additional nil-like edge cases.\n",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        prompts.append(prompt_path.read_text(encoding="utf-8"))
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    def fake_ensure_worktree(path: Path, branch: str):
        ensured["path"] = path
        ensured["branch"] = branch

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeResumeGH)
    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff"
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )
    monkeypatch.setattr("ai_issue_worker.runner.ensure_worktree", fake_ensure_worktree)
    monkeypatch.setattr(
        "ai_issue_worker.runner.commit_all", lambda worktree_path, message: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.push_branch", lambda worktree_path, branch: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.remove_worktree", lambda worktree_path: None
    )

    result = process_issue_resume(
        config,
        issue,
        previous,
        tmp_path,
        {"run_root": run_root},
        manual_note="Address the reviewer feedback without changing the public API.",
    )

    assert result == EXIT_OK
    assert ensured == {"path": worktree_path, "branch": "ai/issue-123-bug-title"}
    assert updated_pr["url"] == previous.pr_url
    assert "Bug title" in updated_pr["title"]
    assert "PASS pytest" in updated_pr["body"]
    assert prompts
    prompt = prompts[0]
    assert "## Continuation context" in prompt
    assert "Address the reviewer feedback without changing the public API." in prompt
    assert "## Prior implementation summary" in prompt
    assert "Added nil handling." in prompt
    assert "Please add a regression test." in prompt
    assert "Handle the nil case too." in prompt
    assert "One more edge case looks untested." in prompt
    assert "Draft PR opened:" not in prompt
    assert "Old review" not in prompt
    summary = (issue_run_dir / "summary.md").read_text(encoding="utf-8")
    assert "Added regression coverage." in summary
    assert "Keep the API stable." in summary


def test_process_issue_resume_skips_stale_summary_after_failed_run(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict(
        {"repo": "owner/repo", "verify": {"commands": ["pytest"]}}
    )
    run_root = tmp_path / "runs"
    worktree_path = tmp_path / "worktree"
    worktree_path.mkdir()
    issue_run_dir = run_root / "issue-123"
    issue_run_dir.mkdir(parents=True)
    (issue_run_dir / "summary.md").write_text(
        "## What changed\n- Stale summary.\n\n## Decisions to preserve\n- Old constraint.\n\n## Follow-up context\n- Old note.\n",
        encoding="utf-8",
    )
    previous = JobRecord(
        issue_number=123,
        issue_title="Bug title",
        branch_name="ai/issue-123-bug-title",
        worktree_path=str(worktree_path),
        status="verify_failed",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:10:00Z",
        base_branch="main",
        pr_url="https://github.com/owner/repo/pull/5",
    )
    issue = Issue(123, "Bug title", "Bug body", ["ai-pr-opened"], "open")
    prompts: list[str] = []

    class FakeResumeGH:
        def __init__(self, repo: str):
            self.repo = repo

        def issue_comments(self, number: int):
            return []

        def pr_comments(self, pr_url: str):
            return []

        def pr_reviews(self, pr_url: str):
            return []

        def add_label(self, number: int, label: str) -> None:
            pass

        def remove_label(self, number: int, label: str) -> None:
            pass

        def comment(self, number: int, body_file: Path) -> None:
            pass

        def update_pr(self, pr_url: str, title: str, body_file: Path) -> None:
            pass

    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: NONE\n\nNo findings.",
            "## What changed\n- Refreshed summary.\n\n## Decisions to preserve\n- New constraint.\n\n## Follow-up context\n- New note.\n",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        prompts.append(prompt_path.read_text(encoding="utf-8"))
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeResumeGH)
    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff"
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.ensure_worktree", lambda path, branch: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.commit_all", lambda worktree_path, message: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.push_branch", lambda worktree_path, branch: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.remove_worktree", lambda worktree_path: None
    )

    result = process_issue_resume(
        config,
        issue,
        previous,
        tmp_path,
        {"run_root": run_root},
        manual_note="Address the latest failure.",
    )

    assert result == EXIT_OK
    prompt = prompts[0]
    assert "## Prior implementation summary" not in prompt
    assert "Stale summary." not in prompt


def test_process_issue_writes_summary_for_future_resume(monkeypatch, tmp_path: Path):
    config = config_from_dict(
        {"repo": "owner/repo", "verify": {"commands": ["pytest"]}}
    )
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    paths = {
        "run_root": tmp_path / "runs",
        "worktree_root": tmp_path / "worktrees",
    }
    worktree_path = paths["worktree_root"] / "issue-123"
    worktree_path.mkdir(parents=True)
    prompts: list[str] = []

    class FakeGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str) -> None:
            pass

        def remove_label(self, number: int, label: str) -> None:
            pass

        def comment(self, number: int, body_file: Path) -> None:
            pass

        def create_pr(
            self, base: str, head: str, title: str, body_file: Path, draft: bool = True
        ) -> str:
            assert base == "main"
            assert head == "ai/issue-123-bug-title"
            return "https://github.com/owner/repo/pull/123"

    outputs = iter(
        [
            "implementation complete",
            "BLOCKING_PRIORITIES: NONE\n\nNo findings.",
            "## What changed\n- Added regression coverage.\n\n## Decisions to preserve\n- Keep the API stable.\n\n## Follow-up context\n- Resume from the existing PR if more comments arrive.\n",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        prompts.append(prompt_path.read_text(encoding="utf-8"))
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeGH)
    monkeypatch.setattr(
        "ai_issue_worker.runner.unique_branch_name",
        lambda git_config, number, title: "ai/issue-123-bug-title",
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.add_worktree", lambda path, branch, base_branch: None
    )
    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr(
        "ai_issue_worker.runner._diff_snapshot", lambda worktree_path: "diff"
    )
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.commit_all", lambda worktree_path, message: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.push_branch", lambda worktree_path, branch: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.remove_worktree", lambda worktree_path: None
    )

    result = process_issue(
        config,
        IssueWorkPlan(issue=issue, base_branch="main"),
        tmp_path,
        paths,
    )

    assert result == EXIT_OK
    assert any("Resume summary task" in prompt for prompt in prompts)
    summary = (paths["run_root"] / "issue-123" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert "Added regression coverage." in summary
    assert "Keep the API stable." in summary


def test_process_issue_writes_summary_when_review_is_disabled(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict(
        {
            "repo": "owner/repo",
            "verify": {"commands": ["pytest"]},
            "review": {"enabled": False},
        }
    )
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    paths = {
        "run_root": tmp_path / "runs",
        "worktree_root": tmp_path / "worktrees",
    }
    worktree_path = paths["worktree_root"] / "issue-123"
    worktree_path.mkdir(parents=True)
    prompt_targets: list[Path] = []
    commands: list[str | None] = []

    class FakeGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str) -> None:
            pass

        def remove_label(self, number: int, label: str) -> None:
            pass

        def comment(self, number: int, body_file: Path) -> None:
            pass

        def create_pr(
            self, base: str, head: str, title: str, body_file: Path, draft: bool = True
        ) -> str:
            return "https://github.com/owner/repo/pull/123"

    outputs = iter(
        [
            "implementation complete",
            "## What changed\n- Added regression coverage.\n\n## Decisions to preserve\n- Keep the API stable.\n\n## Follow-up context\n- Resume from the existing PR if more comments arrive.\n",
        ]
    )

    def fake_codex(config, worktree_path, prompt_path, log_path, command=None):
        prompt_targets.append(worktree_path)
        commands.append(command)
        log_path.write_text("log", encoding="utf-8")
        return AgentResult(True, 0, next(outputs), "", 0.1, False)

    def fake_verifier(config, worktree_path, log_path):
        log_path.write_text("PASS pytest", encoding="utf-8")
        return VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 0.1)])

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeGH)
    monkeypatch.setattr(
        "ai_issue_worker.runner.unique_branch_name",
        lambda git_config, number, title: "ai/issue-123-bug-title",
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.add_worktree", lambda path, branch, base_branch: None
    )
    monkeypatch.setattr("ai_issue_worker.runner._run_codex_session", fake_codex)
    monkeypatch.setattr("ai_issue_worker.runner.run_verifier", fake_verifier)
    monkeypatch.setattr(
        "ai_issue_worker.runner.inspect_diff", lambda worktree_path, config: _diff()
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.commit_all", lambda worktree_path, message: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.push_branch", lambda worktree_path, branch: None
    )
    monkeypatch.setattr(
        "ai_issue_worker.runner.remove_worktree", lambda worktree_path: None
    )

    result = process_issue(
        config,
        IssueWorkPlan(issue=issue, base_branch="main"),
        tmp_path,
        paths,
    )

    assert result == EXIT_OK
    assert commands == [None, None]
    assert prompt_targets[-1] == paths["run_root"] / "issue-123"
    summary = (paths["run_root"] / "issue-123" / "summary.md").read_text(
        encoding="utf-8"
    )
    assert "Added regression coverage." in summary


def test_run_once_dispatches_queued_resume(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("repo: owner/repo\n", encoding="utf-8")
    run_root = tmp_path / ".ai-runs"
    _write_pr_job(run_root, 77, "ai/issue-77-existing")
    captured = {}

    class FakeRunOnceGH:
        def __init__(self, repo: str):
            self.repo = repo

        def validate(self) -> None:
            pass

        def list_issues(self, labels):
            captured["labels"] = labels
            return [
                Issue(
                    77,
                    "Resume",
                    "",
                    ["ai-pr-opened", "ai-resume"],
                    "open",
                    updated_at="2026-01-01T00:00:00Z",
                )
            ]

        def view_issue(self, number: int):
            assert number == 77
            return Issue(
                77,
                "Resume",
                "Issue body",
                ["ai-pr-opened", "ai-resume"],
                "open",
                updated_at="2026-01-01T00:00:00Z",
            )

        def blocked_by(self, number: int):
            return []

    def fake_resume(config, issue, previous_record, repo_root, paths, manual_note=""):
        captured["issue"] = issue.number
        captured["branch"] = previous_record.branch_name
        captured["manual_note"] = manual_note
        return EXIT_OK

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeRunOnceGH)
    monkeypatch.setattr(
        "ai_issue_worker.runner.check_dependencies",
        lambda config, root=None, paths=None: None,
    )
    monkeypatch.setattr("ai_issue_worker.runner.process_issue_resume", fake_resume)

    result = run_once(config_path)

    assert result == EXIT_OK
    assert captured["labels"] == ["ai-ready", "ai-resume"]
    assert captured["issue"] == 77
    assert captured["branch"] == "ai/issue-77-existing"
    assert captured["manual_note"] == ""


def test_run_once_dispatches_parent_issue(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("repo: owner/repo\n", encoding="utf-8")
    captured = {}

    class FakeRunOnceParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def validate(self) -> None:
            pass

        def list_issues(self, labels):
            return [
                Issue(
                    88,
                    "Parent",
                    "",
                    ["ai-ready", "ai-parent"],
                    "open",
                    updated_at="2026-01-01T00:00:00Z",
                )
            ]

        def view_issue(self, number: int):
            return Issue(88, "Parent", "Parent body", ["ai-ready", "ai-parent"], "open")

        def blocked_by(self, number: int):
            return []

    def fake_parent(config, plan, repo_root, paths):
        captured["issue"] = plan.issue.number
        captured["mode"] = plan.mode
        return EXIT_OK

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeRunOnceParentGH)
    monkeypatch.setattr(
        "ai_issue_worker.runner.check_dependencies",
        lambda config, root=None, paths=None: None,
    )
    monkeypatch.setattr("ai_issue_worker.runner.process_parent_issue", fake_parent)

    result = run_once(config_path)

    assert result == EXIT_OK
    assert captured == {"issue": 88, "mode": "parent"}


def test_process_parent_issue_runs_up_to_configured_child_limit(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"max_parent_children_per_run": 3}}
    )
    parent = Issue(50, "Parent", "Parent body", ["ai-ready", "ai-parent"], "open")
    children = [
        Issue(51, "Child 1", "Body 1", ["ai-child"], "open"),
        Issue(52, "Child 2", "Body 2", ["ai-child"], "open"),
        Issue(53, "Child 3", "Body 3", ["ai-child"], "open"),
        Issue(54, "Child 4", "Body 4", ["ai-child"], "open"),
    ]
    paths = {
        "run_root": tmp_path / ".ai-runs",
        "worktree_root": tmp_path / ".ai-worktrees",
    }
    captured = {"processed": [], "removed": [], "added": []}

    class FakeParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str):
            captured["added"].append((number, label))

        def remove_label(self, number: int, label: str):
            captured["removed"].append((number, label))

        def comment(self, number: int, body_file: Path):
            captured.setdefault("comments", []).append(
                (number, body_file.read_text(encoding="utf-8"))
            )

        def sub_issues(self, number: int):
            return children

        def blocked_by(self, number: int):
            return []

    def fake_process_issue(config, plan, repo_root, paths, follow_up=""):
        captured["processed"].append(plan.issue.number)
        _write_pr_job(
            paths["run_root"], plan.issue.number, f"ai/issue-{plan.issue.number}"
        )
        run_dir = paths["run_root"] / f"issue-{plan.issue.number}"
        (run_dir / "summary.md").write_text(
            f"Summary for {plan.issue.number}", encoding="utf-8"
        )
        return EXIT_OK

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeParentGH)
    monkeypatch.setattr("ai_issue_worker.runner.process_issue", fake_process_issue)

    result = process_parent_issue(
        config, IssueWorkPlan(parent, "main", mode="parent"), tmp_path, paths
    )

    assert result == EXIT_OK
    assert captured["processed"] == [51, 52, 53]
    assert (50, "ai-working") in captured["removed"]
    record = load_job_record(paths["run_root"] / "issue-50" / "latest.json")
    assert record.status == "parent_partial"


def test_process_parent_issue_stops_on_child_failure(monkeypatch, tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo"})
    parent = Issue(60, "Parent", "Parent body", ["ai-ready", "ai-parent"], "open")
    child = Issue(61, "Child", "Body", ["ai-child"], "open")
    paths = {
        "run_root": tmp_path / ".ai-runs",
        "worktree_root": tmp_path / ".ai-worktrees",
    }
    captured = {"added": [], "comments": []}

    class FakeParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str):
            captured["added"].append((number, label))

        def remove_label(self, number: int, label: str):
            pass

        def comment(self, number: int, body_file: Path):
            captured["comments"].append(body_file.read_text(encoding="utf-8"))

        def sub_issues(self, number: int):
            return [child]

        def blocked_by(self, number: int):
            return []

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeParentGH)
    monkeypatch.setattr(
        "ai_issue_worker.runner.process_issue", lambda *args, **kwargs: EXIT_VERIFY
    )

    result = process_parent_issue(
        config, IssueWorkPlan(parent, "main", mode="parent"), tmp_path, paths
    )

    assert result == EXIT_VERIFY
    assert (60, "ai-failed") in captured["added"]
    assert "child issue #61 failed" in captured["comments"][-1]
    record = load_job_record(paths["run_root"] / "issue-60" / "latest.json")
    assert record.status == "parent_failed"


def test_process_parent_issue_passes_parent_memory_to_later_children(
    monkeypatch, tmp_path: Path
):
    config = config_from_dict({"repo": "owner/repo"})
    parent = Issue(70, "Parent", "Parent body", ["ai-ready", "ai-parent"], "open")
    children = [
        Issue(71, "Child 1", "Body 1", ["ai-child"], "open"),
        Issue(72, "Child 2", "Body 2", ["ai-child"], "open"),
    ]
    paths = {
        "run_root": tmp_path / ".ai-runs",
        "worktree_root": tmp_path / ".ai-worktrees",
    }
    follow_ups = []

    class FakeParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str):
            pass

        def remove_label(self, number: int, label: str):
            pass

        def comment(self, number: int, body_file: Path):
            pass

        def sub_issues(self, number: int):
            return children

        def blocked_by(self, number: int):
            return []

    def fake_process_issue(config, plan, repo_root, paths, follow_up=""):
        follow_ups.append(follow_up)
        _write_pr_job(
            paths["run_root"], plan.issue.number, f"ai/issue-{plan.issue.number}"
        )
        run_dir = paths["run_root"] / f"issue-{plan.issue.number}"
        (run_dir / "summary.md").write_text(
            f"Summary for child {plan.issue.number}", encoding="utf-8"
        )
        return EXIT_OK

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeParentGH)
    monkeypatch.setattr("ai_issue_worker.runner.process_issue", fake_process_issue)

    result = process_parent_issue(
        config, IssueWorkPlan(parent, "main", mode="parent"), tmp_path, paths
    )

    assert result == EXIT_OK
    assert "Parent body" in follow_ups[0]
    assert "Child #71" in follow_ups[1]
    assert "Summary for child 71" in follow_ups[1]


def test_process_parent_issue_respects_stacked_pr_setting(monkeypatch, tmp_path: Path):
    config = config_from_dict(
        {"repo": "owner/repo", "issue_selection": {"allow_stacked_prs": False}}
    )
    parent = Issue(80, "Parent", "Parent body", ["ai-ready", "ai-parent"], "open")
    blocker = Issue(81, "Blocker", "Body", ["ai-child"], "open")
    dependent = Issue(82, "Dependent", "Body", ["ai-child"], "open")
    paths = {
        "run_root": tmp_path / ".ai-runs",
        "worktree_root": tmp_path / ".ai-worktrees",
    }
    processed = []

    class FakeParentGH:
        def __init__(self, repo: str):
            self.repo = repo

        def add_label(self, number: int, label: str):
            pass

        def remove_label(self, number: int, label: str):
            pass

        def comment(self, number: int, body_file: Path):
            pass

        def sub_issues(self, number: int):
            return [blocker, dependent]

        def blocked_by(self, number: int):
            return [blocker] if number == dependent.number else []

    def fake_process_issue(config, plan, repo_root, paths, follow_up=""):
        processed.append(plan.issue.number)
        _write_pr_job(
            paths["run_root"], plan.issue.number, f"ai/issue-{plan.issue.number}"
        )
        return EXIT_OK

    monkeypatch.setattr("ai_issue_worker.runner.GHClient", FakeParentGH)
    monkeypatch.setattr("ai_issue_worker.runner.process_issue", fake_process_issue)

    result = process_parent_issue(
        config, IssueWorkPlan(parent, "main", mode="parent"), tmp_path, paths
    )

    assert result == EXIT_OK
    assert processed == [81]
    record = load_job_record(paths["run_root"] / "issue-80" / "latest.json")
    assert record.status == "parent_partial"


def test_diff_snapshot_changes_when_untracked_file_content_changes(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "tracked.txt"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True
    )

    untracked = tmp_path / "new.txt"
    untracked.write_text("before\n", encoding="utf-8")
    before = _diff_snapshot(tmp_path)
    untracked.write_text("after\n", encoding="utf-8")
    after = _diff_snapshot(tmp_path)

    assert before != after


def test_diff_snapshot_changes_when_ignored_file_content_changes(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    (tmp_path / ".gitignore").write_text(".env\nignored/\n", encoding="utf-8")
    (tmp_path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", ".gitignore", "tracked.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True
    )

    ignored = tmp_path / ".env"
    ignored.write_text("before\n", encoding="utf-8")
    before = _diff_snapshot(tmp_path)
    ignored.write_text("after\n", encoding="utf-8")
    after = _diff_snapshot(tmp_path)

    assert before != after
