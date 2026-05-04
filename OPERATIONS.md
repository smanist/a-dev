# Operations

## Core Commands

Bootstrap config and labels:

```bash
ai-issue init
```

Inspect candidate issues:

```bash
ai-issue list
```

Run one local execution cycle:

```bash
ai-issue run-once
```

Create a new `ai-ready` issue from rough notes:

```bash
ai-issue create --title "Fix parser crash" "Parser crashes when input is empty."
```

Force the issue shape when needed:

```bash
ai-issue create --mode single "Small localized fix."
ai-issue create --mode parent --description-file larger-change.md
```

Start or stop the background loop:

```bash
ai-issue start
ai-issue status
ai-issue logs
ai-issue stop
```

Inspect local worker state:

```bash
ai-issue inspect
```

Retry a failed issue:

```bash
ai-issue retry <issue-number>
```

Resume an existing ai-issue PR with follow-up instructions:

```bash
ai-issue resume <issue-number> --comment "Address the latest review feedback."
```

Queue an existing ai-issue PR for the normal scheduler to pick up later:

```bash
ai-issue resume <issue-number> --queue --comment "Address the latest review feedback."
```

Clean old run directories and worktrees:

```bash
ai-issue clean --older-than 7d
```

## Config Fields That Matter Most

The highest-leverage config sections in `.ai-issue-worker.yaml` are:

- `issue_selection`: labels, dependency behavior, stacked PR support, and ordering.
- `agent`: Codex command, model, reasoning, timeout, and repair attempts.
- `review`: whether review is enabled, the read-only review command, and blocking priorities.
- `verify`: commands run inside the worktree after implementation or review fixes.
- `diff_policy`: file-count, diff-size, lockfile, and rejected-path guardrails.
- `git`: branch prefix, cleanup behavior, dirty-base tolerance, and commit-message template.
- `pr`: draft mode and PR title/body templates.

## Where To Look During Debugging

For a specific issue run:

1. Open `.ai-runs/issue-<n>/latest.json` for overall status.
2. Read `.ai-runs/issue-<n>/artifacts.log` for the artifact timeline.
3. Read `.ai-runs/issue-<n>/prompt.md` to see the latest prompt the worker sent.
4. Read `.ai-runs/issue-<n>/codex.log`, `verify.log`, `review.md`, `summary.md`, and `pr_body.md` depending on the failure stage.

For a parent issue run:

1. Open `.ai-runs/issue-<parent>/parent-plan.json` for the current sub-issue DAG snapshot.
2. Read `.ai-runs/issue-<parent>/parent-memory.md` for accumulated child PR summaries and decisions.
3. Inspect each child issue directory for its normal `latest.json`, `summary.md`, `verify.log`, and PR body.

For daemon state:

1. Read `.ai-runtime/worker.status.json`.
2. Read `.ai-logs/worker.log`.
3. Check `.ai-runtime/worker.lock` and `.ai-runtime/worker.pid`.

## Common Failure Classes

- `EXIT_CONFIG`: invalid or missing config.
- `EXIT_DEPENDENCY`: `gh`, `git`, or Codex command is unavailable.
- `EXIT_GH`: GitHub auth or API failure.
- `EXIT_GIT`: dirty base checkout, fetch/worktree failure, commit/push failure.
- `EXIT_AGENT`: implementation, repair, or review Codex session failed.
- `EXIT_VERIFY`: verifier failed, diff policy rejected changes, or no useful diff was produced.
- `EXIT_PR`: draft PR creation failed.
- `EXIT_LOCK`: another worker instance already holds the runtime lock.

## Behavioral Notes

- Review is a second-pass gate, not a formatter. It should report findings without editing files.
- Stacked PRs are only considered when dependency checking is enabled, there is exactly one open blocker, and that blocker already has a recorded `pr_opened` job.
- Parent issues carry `ai-parent` and orchestrate `ai-child` sub-issues. Children remain the code-producing PR units.
- Parent runs process children serially up to `issue_selection.max_parent_children_per_run` and leave the parent `ai-ready` if blocked or only partially drained.
- `clean --delete-local-branches` deletes local branches with `git branch -D`; use it deliberately.
- `keep_worktree_on_failure` and `keep_worktree_on_success` change how much state remains available for inspection after runs.
- `resume` without `--queue` bypasses normal `ai-ready` selection. It reuses the recorded branch/worktree for an existing PR, pulls in the latest local resume summary plus new issue comments and PR comments/reviews since the last run, and updates the existing PR body after verification.
- `resume --queue` posts the optional note as an issue comment, adds the `ai-resume` label, and lets the normal `run-once` / `start` loop pick that PR revision up later.

## Test Workflow

Fast validation:

```bash
pytest
```

Full verifier-style validation when local tools are installed:

```bash
ruff check .
ruff format --check .
pyright
pytest
```
