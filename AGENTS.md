# Agent Guide

## Purpose

This repository is a local automation worker that turns GitHub issues into draft pull requests. The normal single-issue loop is:

1. Select one `ai-ready` issue from GitHub.
2. Create an isolated git worktree and branch.
3. Ask Codex to implement the fix.
4. Run verifier commands and limited repair attempts.
5. Run a separate Codex review loop and fix blocking findings.
6. Enforce diff policy, commit, push, and open a draft PR.

For larger requests, `ai-issue create --mode parent` can create a schedulable
parent issue with GitHub sub-issues and native `blocked_by` dependency edges.
When the worker selects an `ai-ready` + `ai-parent` issue, it orchestrates
eligible child issues serially. Each child still gets its own worktree, Codex
session, verification/review loop, commit, push, and draft PR.

The repo is intentionally small. Most behavior lives in `src/ai_issue_worker/runner.py` and `src/ai_issue_worker/cli.py`.

## High-Signal Files

- `src/ai_issue_worker/runner.py`: orchestration for one issue run, including verification, review, diff checks, commit/push, and PR creation.
- `src/ai_issue_worker/cli.py`: user-facing commands such as `init`, `list`, `create`, `run-once`, `start`, `inspect`, `retry`, `resume`, and `clean`.
- `src/ai_issue_worker/config.py`: config schema, defaults, and validation.
- `src/ai_issue_worker/prompt.py`: prompts sent to Codex. This is where repo instructions are assembled.
- `src/ai_issue_worker/worktree.py`: git safety checks, branch naming, worktree creation/removal, commit, and push.
- `src/ai_issue_worker/github_gh.py`: all GitHub access via `gh`.
- `src/ai_issue_worker/verifier.py`: verifier command execution and summary formatting.
- `src/ai_issue_worker/diff_policy.py`: post-edit guardrails on changed files, diff size, rejected paths, and lockfiles.
- `src/ai_issue_worker/jobs.py`: run artifacts, latest-file copies, and job record persistence.
- `tests/`: behavior-focused tests that are usually the fastest way to confirm intent.

## Runtime Model

- Config is loaded from `.ai-issue-worker.yaml`.
- Local state lives under `.ai-worktrees`, `.ai-runs`, `.ai-logs`, and `.ai-runtime`.
- The worker uses filesystem artifacts as its audit trail. Each issue gets a run directory under `.ai-runs/issue-<n>/`.
- Successful PR open/update flows also write a local `summary.md` artifact for future resume runs.
- Parent runs write `parent-plan.json` and `parent-memory.md` in the parent run directory to carry durable context across child Codex sessions.
- `latest.json`, `prompt.md`, `verify.log`, `review.md`, `summary.md`, `pr_body.md`, and `codex.log` are convenience pointers to the newest timestamped artifacts.
- The worker lock is `.ai-runtime/worker.lock`. Daemon status is `.ai-runtime/worker.status.json`.

## Invariants To Preserve

- `runner.py` owns lifecycle and exit-code decisions. Keep failure labeling, cleanup, and job-record updates coherent.
- Review runs must not edit the worktree. `_diff_snapshot()` is the enforcement mechanism.
- The outer worker commits and pushes. Prompts explicitly instruct the inner Codex session not to do that.
- Resume runs for existing PRs must update the existing PR and recorded branch/worktree instead of silently opening a second PR.
- Queued resume runs are selected via the `ai-resume` label and should stay distinct from fresh `ai-ready` issue work.
- Parent issues are orchestration-only; child issues are the code-producing PR units.
- Parent runs process children serially and must respect the existing stacked PR settings for open blockers.
- GitHub comments, issue bodies, and PR bodies must be sanitized before leaving the machine.
- Diff policy is enforced after implementation and review loops succeed. Do not bypass it accidentally.
- `allow_dirty_base` only tolerates worker-owned runtime paths unless explicitly configured otherwise.
- Stacked PR behavior depends on prior `latest.json` records for blocker issues.

## Safe Change Patterns

- If you change orchestration flow, read `tests/test_runner_review.py` first and keep the review/repair semantics intact.
- If you change CLI behavior, update `tests/test_cli.py`.
- If you change git or GitHub interactions, update `tests/test_worktree.py` or `tests/test_github_gh.py`.
- If you add new agent-facing docs, keep `prompt.repository_instructions()` in sync so Codex sessions can see them.
- If a code change alters repo behavior, operator workflow, control flow, invariants, or supported commands, update the relevant markdown docs in this repo, especially `AGENTS.md`, `ARCHITECTURE.md`, `OPERATIONS.md`, and `README.md`.

## Local Validation

Primary test command:

```bash
pytest
```

The configured verifier defaults are stricter than the test suite:

```bash
ruff check .
ruff format --check .
pyright
pytest
```

## Deeper Docs

- `ARCHITECTURE.md`: control flow, module boundaries, data flow, and extension points.
- `OPERATIONS.md`: CLI commands, artifacts, debugging workflow, and common operator tasks.
