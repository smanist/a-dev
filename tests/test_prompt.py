from pathlib import Path

from ai_issue_worker.config import config_from_dict
from ai_issue_worker.models import CommandResult, DiffSummary, Issue, VerifyResult
from ai_issue_worker.prompt import (
    build_issue_draft_prompt,
    build_prompt,
    build_resume_summary_prompt,
    build_review_fix_prompt,
    build_review_prompt,
    repository_instructions,
)


def test_prompt_contains_issue_and_constraints(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "verify": {"commands": ["pytest"]}})
    (tmp_path / "AGENTS.md").write_text("Follow local instructions.", encoding="utf-8")
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    prompt = build_prompt(issue, config, tmp_path)
    assert "Fix GitHub issue #123" in prompt
    assert "Bug title" in prompt
    assert "Bug body" in prompt
    assert "`pytest`" in prompt
    assert "Do not commit changes" in prompt
    assert "Do not create branches or pull requests" in prompt


def test_prompt_includes_follow_up_context_when_present(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "verify": {"commands": ["pytest"]}})
    (tmp_path / "AGENTS.md").write_text("Follow local instructions.", encoding="utf-8")
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")

    prompt = build_prompt(issue, config, tmp_path, follow_up="Continue on the existing PR.\n\nAddress the reviewer note.")

    assert "## Continuation context" in prompt
    assert "Continue on the existing PR." in prompt
    assert "Address the reviewer note." in prompt


def test_review_prompt_requests_structured_blocking_priorities(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo", "review": {"fix_priorities": ["P0", "P1"]}})
    (tmp_path / "AGENTS.md").write_text("Follow local instructions.", encoding="utf-8")
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    diff = DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)
    verify = VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 1.0)])

    prompt = build_review_prompt(issue, config, tmp_path, diff, verify)

    assert "Review the current working tree changes for GitHub issue #123" in prompt
    assert "Do not edit files" in prompt
    assert "BLOCKING_PRIORITIES: NONE" in prompt
    assert "Treat only P0, P1 as blocking" in prompt
    assert "src/app.py" in prompt
    assert "PASS pytest" in prompt
    assert "Follow local instructions." in prompt


def test_review_fix_prompt_targets_only_blocking_findings():
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    diff = DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)

    prompt = build_review_fix_prompt(issue, "[P1] Broken edge case", diff, ["P1"])

    assert "Review fix task" in prompt
    assert "Bug title" in prompt
    assert "[P1] Broken edge case" in prompt
    assert "Fix only review findings with these priorities: P1" in prompt
    assert "Do not address findings with other priorities" in prompt
    assert "Do not commit changes" in prompt


def test_resume_summary_prompt_reuses_prior_summary_and_requires_sections():
    issue = Issue(123, "Bug title", "Bug body", ["ai-ready"], "open")
    diff = DiffSummary(["src/app.py"], " src/app.py | 2 +-", 2, False, None)
    verify = VerifyResult(True, [CommandResult("pytest", 0, "ok", "", 1.0)])

    prompt = build_resume_summary_prompt(
        issue,
        diff,
        verify,
        previous_summary="## What changed\n- Old point\n",
    )

    assert "Resume summary task" in prompt
    assert "## Previous resume summary" in prompt
    assert "Old point" in prompt
    assert "Use exactly these sections" in prompt
    assert "## What changed" in prompt
    assert "## Decisions to preserve" in prompt
    assert "## Follow-up context" in prompt


def test_issue_draft_prompt_requires_json_output(tmp_path: Path):
    config = config_from_dict({"repo": "owner/repo"})
    (tmp_path / "README.md").write_text("Parser project.", encoding="utf-8")

    prompt = build_issue_draft_prompt("parser crashes on empty input", tmp_path, config, title_hint="Parser crash on empty input")

    assert "Return only valid JSON" in prompt
    assert '"title"' in prompt
    assert '"body"' in prompt
    assert "parser crashes on empty input" in prompt
    assert "Title hint: Parser crash on empty input" in prompt
    assert "Parser project." in prompt


def test_repository_instructions_prefers_agents_only_when_present(tmp_path: Path):
    (tmp_path / "AGENTS.md").write_text("Agent instructions.", encoding="utf-8")
    (tmp_path / "ARCHITECTURE.md").write_text("Architecture notes.", encoding="utf-8")
    (tmp_path / "OPERATIONS.md").write_text("Operations notes.", encoding="utf-8")
    (tmp_path / "README.md").write_text("Readme notes.", encoding="utf-8")

    instructions = repository_instructions(tmp_path)

    assert "## AGENTS.md" in instructions
    assert "Agent instructions." in instructions
    assert "## README.md" not in instructions
    assert "Architecture notes." not in instructions
    assert "Operations notes." not in instructions


def test_repository_instructions_fall_back_to_readme_when_agents_missing(tmp_path: Path):
    (tmp_path / "README.md").write_text("Readme notes.", encoding="utf-8")

    instructions = repository_instructions(tmp_path)

    assert "## README.md" in instructions
    assert "Readme notes." in instructions
