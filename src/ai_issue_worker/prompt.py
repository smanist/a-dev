from __future__ import annotations

from pathlib import Path

from .config import WorkerConfig
from .models import DiffSummary, Issue, VerifyResult
from .verifier import format_verification_summary


MAX_INSTRUCTION_CHARS = 12_000
MAX_RESUME_SUMMARY_CHARS = 6_000


def repository_instructions(repo_root: Path) -> str:
    agents_path = repo_root / "AGENTS.md"
    if agents_path.exists() and agents_path.is_file():
        text = agents_path.read_text(encoding="utf-8", errors="replace")
        return f"## AGENTS.md\n\n{text.strip()}"[:MAX_INSTRUCTION_CHARS]

    parts: list[str] = []
    for name, limit in {
        "CONTRIBUTING.md": 2_000,
        "README.md": 3_000,
        "pyproject.toml": 2_000,
    }.items():
        path = repo_root / name
        if path.exists() and path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            snippet = text if limit is None else text[:limit]
            parts.append(f"## {name}\n\n{snippet.strip()}")
    joined = "\n\n".join(parts).strip()
    return (
        joined[:MAX_INSTRUCTION_CHARS]
        if joined
        else "No repository instruction files were found."
    )


def build_prompt(
    issue: Issue, config: WorkerConfig, repo_root: Path, follow_up: str = ""
) -> str:
    verify_commands = "\n".join(f"- `{command}`" for command in config.verify.commands)
    follow_up_section = ""
    if follow_up.strip():
        follow_up_section = f"""
## Continuation context

{follow_up.strip()}
"""
    return f"""# Task

You are working in a local git worktree for repository `{config.repo}`.

Fix GitHub issue #{issue.number}.

## Issue title

{issue.title}

## Issue body

{issue.body}
{follow_up_section}

## Constraints

- Make the minimal code change needed to resolve the issue.
- Do not modify unrelated files.
- Do not change public APIs unless necessary.
- Add or update tests when appropriate.
- Prefer simple, maintainable changes.
- Do not commit changes. The outer worker will commit.
- Do not create branches or pull requests. The outer worker will do that.
- Stop after editing files. The outer worker will run verification.

## Repository verification commands

The outer worker will run:

{verify_commands}

## Repository instructions

{repository_instructions(repo_root)}

## Expected deliverable

A modified working tree that addresses the issue.
"""


def build_issue_draft_prompt(
    description: str,
    repo_root: Path,
    config: WorkerConfig,
    title_hint: str | None = None,
    mode: str = "auto",
) -> str:
    title_note = f"- Title hint: {title_hint}\n" if title_hint else ""
    mode_note = {
        "auto": "Choose `single` for small localized work. Choose `parent` when the work naturally needs multiple dependent issues.",
        "single": "Return `single`; do not split the request into sub-issues.",
        "parent": "Return `parent`; split the request into a parent tracking issue and implementation sub-issues.",
    }.get(mode, "Choose the most appropriate issue shape.")
    return f"""# Task

You are drafting a GitHub issue for repository `{config.repo}` from rough local notes.

Turn the rough notes into concise, implementation-ready GitHub issue content.

## Rough notes

{description}

## Requirements

- Return only valid JSON. Do not wrap it in markdown fences.
- Create mode: `{mode}`.
- {mode_note}
- For a single issue, output shape: {{"kind": "single", "issue": {{"title": "...", "body": "..."}}}}
- For a parent issue, output shape: {{"kind": "parent", "parent": {{"title": "...", "body": "..."}}, "children": [{{"key": "...", "title": "...", "body": "...", "blocked_by": ["..."]}}]}}
- `title` should be concise and specific.
- `body` values must be GitHub-flavored Markdown without a top-level title heading.
- Make every implementation issue concrete enough for an engineering agent to act on.
- Parent issues should describe the overall goal, dependency graph, and shared context.
- Child issue `key` values must be unique stable slugs.
- Child `blocked_by` entries must reference other child keys only.
- Preserve uncertainty explicitly instead of inventing facts.
- Keep bodies focused and not verbose.
{title_note}
## Desired body structure

- `## Summary`
- `## Problem`
- `## Expected outcome`
- `## Acceptance criteria`
- `## Notes`

## Repository instructions

{repository_instructions(repo_root)}
"""


def build_repair_prompt(
    issue: Issue, verify_logs: str, diff_summary: DiffSummary
) -> str:
    return f"""# Repair task

Your previous attempt to fix GitHub issue #{issue.number} did not pass verification.

## Failed verification logs

{verify_logs[-12000:]}

## Current git diff summary

Changed files:
{chr(10).join(diff_summary.changed_files)}

Diff stat:
{diff_summary.diff_stat}

## Instructions

Fix the verification failures with minimal changes.
Do not revert unrelated useful changes unless needed.
Do not commit changes.
"""


def build_review_prompt(
    issue: Issue,
    config: WorkerConfig,
    repo_root: Path,
    diff_summary: DiffSummary,
    verify: VerifyResult,
) -> str:
    blocking = ", ".join(config.review.fix_priorities)
    blocking_examples = list(config.review.fix_priorities)
    if len(config.review.fix_priorities) > 1:
        blocking_examples.append(",".join(config.review.fix_priorities))
    output_examples = "\nor\n".join(
        f"`BLOCKING_PRIORITIES: {priority}`" for priority in blocking_examples
    )
    return f"""# Code review task

You are a separate Codex code-review session for repository `{config.repo}`.

Review the current working tree changes for GitHub issue #{issue.number}. Do not edit files.

## Issue title

{issue.title}

## Issue body

{issue.body}

## Current git diff summary

Changed files:
{chr(10).join(diff_summary.changed_files)}

Diff stat:
{diff_summary.diff_stat}

## Verification summary

{format_verification_summary(verify)}

## Review scope

- Look for correctness bugs, behavioral regressions, data loss risks, security issues, and critical missing tests.
- Prioritize findings as P0, P1, P2, or P3.
- Treat only {blocking} as blocking for the automated fixer loop.
- Do not report speculative issues.
- Do not edit files, commit changes, create branches, or open pull requests.

## Output format

Start with exactly one line:
`BLOCKING_PRIORITIES: NONE`
or
{output_examples}

Then list findings. Use this format for each finding:

`[P1] Short title`
File/line:
Problem:
Fix:

If there are no findings, say so after the `BLOCKING_PRIORITIES: NONE` line.

## Repository instructions

{repository_instructions(repo_root)}
"""


def build_review_fix_prompt(
    issue: Issue,
    review_output: str,
    diff_summary: DiffSummary,
    fix_priorities: list[str],
) -> str:
    blocking = ", ".join(fix_priorities)
    return f"""# Review fix task

A separate Codex code-review session found blocking issues in the current implementation for GitHub issue #{issue.number}.

## Issue title

{issue.title}

## Issue body

{issue.body}

## Blocking review output

{review_output[-12000:]}

## Current git diff summary

Changed files:
{chr(10).join(diff_summary.changed_files)}

Diff stat:
{diff_summary.diff_stat}

## Instructions

- Fix only review findings with these priorities: {blocking}.
- Keep the changes minimal and targeted.
- Do not address findings with other priorities unless required to fix a configured blocking issue.
- Do not revert unrelated useful changes unless needed.
- Do not commit changes, create branches, or open pull requests.
"""


def build_resume_summary_prompt(
    issue: Issue,
    diff_summary: DiffSummary,
    verify: VerifyResult,
    previous_summary: str = "",
) -> str:
    previous_summary_section = ""
    if previous_summary.strip():
        previous_summary_section = f"""
## Previous resume summary

{previous_summary.strip()[:MAX_RESUME_SUMMARY_CHARS]}
"""
    return f"""# Resume summary task

You are preparing a short local summary for a future resume session on GitHub issue #{issue.number}. Do not edit files.

## Issue title

{issue.title}

## Issue body

{issue.body}
{previous_summary_section}

## Current git diff summary

Changed files:
{chr(10).join(diff_summary.changed_files)}

Diff stat:
{diff_summary.diff_stat}

## Verification summary

{format_verification_summary(verify)}

## Instructions

- Summarize the current implementation state for the next resume session.
- Prefer concrete decisions, constraints, and follow-up context over restating the issue.
- Replace outdated points from the previous summary instead of preserving stale context.
- Keep the response concise and in Markdown.
- Do not edit files, commit changes, create branches, or open pull requests.

## Output format

Use exactly these sections:

## What changed
- ...

## Decisions to preserve
- ...

## Follow-up context
- ...
"""
