# Operations

## Core Commands

Bootstrap config and labels:

```bash
a-dev init
```

`init` writes `.a-dev.yaml`, ensures `.a-dev/` is ignored, creates bundled
`.vscode/settings.json` and `.vscode/tasks.json` files when missing, and creates
or updates automation labels unless `--no-create-labels` is passed. Use
`--force` to overwrite existing config and VS Code template files.

Inspect candidate issues:

```bash
a-dev list
```

Inspect open issues, all issues, open pull requests, or all pull requests:

```bash
a-dev list --open-issues
a-dev list --all-issues
a-dev list --open-prs
a-dev list --prs
```

Run one local execution cycle:

```bash
a-dev run-once
```

Create a new `ai-ready` issue from rough notes:

```bash
a-dev create --title "Fix parser crash" "Parser crashes when input is empty."
```

For editor-driven notes, put the title in the first line and create from the
current file:

```markdown
Title: Fix parser crash

Parser crashes when input is empty.
```

```bash
a-dev create --description-file issue.md
```

For temporary notes that should not be saved first:

```bash
a-dev create --input-editor
```

Force the issue shape when needed:

```bash
a-dev create --mode single "Small localized fix."
a-dev create --mode parent --description-file larger-change.md
```

Start or stop the background loop:

```bash
a-dev start
a-dev status
a-dev logs
a-dev stop
```

`a-dev status` reports both daemon health and job health. It shows whether the
daemon is running, whether the worker lock is held, the latest local job record,
any unfinished `working` job, open `ai-working` issues, and diagnostics for
common interrupted states such as Codex finishing before verification starts.
Use `a-dev status --json` when another scheduler needs the same state in a
machine-readable form.

Inspect local worker state:

```bash
a-dev inspect
```

Generate an Obsidian-friendly kanban checklist from `.a-dev/runs`:

```bash
a-dev kanban
```

Enable an issue for worker selection. This adds `ai-ready` and removes
`ai-failed`, so it also replaces the old retry flow. It leaves configured
blocked labels such as `blocked` and `needs-human` in place so dependency
constraints remain separate:

```bash
a-dev enable <issue-number>
```

Disable an issue temporarily. This removes `ai-ready` and any queued
`ai-resume` label so neither fresh work nor queued PR continuation will be
selected by the scheduler:

```bash
a-dev disable <issue-number>
```

Reset an issue for a fresh worker run during debugging:

```bash
a-dev reset <issue-number>
```

`reset` closes recorded A-Dev PRs for the issue, requests branch deletion from
GitHub, removes local worker worktrees and branches, deletes the issue run
directory, clears A-Dev lifecycle labels, and adds `ai-ready`. Because GitHub PR
records cannot be deleted, reset closes them. Use `--dry-run` to preview the
cleanup before applying it.

Resume an existing A-Dev PR with follow-up instructions:

```bash
a-dev resume <issue-number> --comment "Address the latest review feedback."
```

Queue an existing A-Dev PR for the normal scheduler to pick up later:

```bash
a-dev resume <issue-number> --queue --comment "Address the latest review feedback."
```

Merge an A-Dev PR after local review, manual commits, and push:

```bash
a-dev merge <issue-number> --method merge
```

`merge` uses the latest local `pr_opened` run record for that issue. It refuses
to proceed if the matching local branch exists but is dirty or differs from
`origin/<branch>`, then merges the PR on GitHub, removes A-Dev PR/resume labels,
records `pr_merged`, and deletes the local and remote branch unless
`--keep-branch` is set. After an immediate merge, it fast-forward pulls the base
branch when the local checkout is on that branch. Use `--method squash` or
`--method rebase` for alternate GitHub merge modes, `--auto` when branch
protection should merge the PR after requirements pass, `--admin` for an
administrator bypass, `--ready` to mark a draft PR ready before merging, and
`--dry-run` to preview the target PR and branch. Auto-merge records
`pr_auto_merge_enabled` and keeps the branch because the PR has not merged yet.

Clean old run directories and worktrees:

```bash
a-dev clean --older-than 7d
```

## Config Fields That Matter Most

The highest-leverage config sections in `.a-dev.yaml` are:

- `issue_selection`: labels, dependency behavior, stacked PR support, and ordering.
- `agent`: Codex command, model, reasoning, timeout, and repair attempts.
- `review`: whether review is enabled, the read-only review command, and blocking priorities.
- `verify`: commands run inside the worktree after implementation or review fixes.
- `diff_policy`: file-count, diff-size, lockfile, and rejected-path guardrails.
- `git`: branch prefix, cleanup behavior, dirty-base tolerance, and commit-message template.
- `pr`: draft mode and PR title/body templates.

## Where To Look During Debugging

For a specific issue run:

1. Run `a-dev status` to check the daemon, worker lock, latest job phase, and interruption diagnostics.
2. Open `.a-dev/runs/issue-<n>/latest.json` for overall status and phase.
3. Read `.a-dev/runs/issue-<n>/artifacts.log` for the artifact timeline.
4. Read `.a-dev/runs/issue-<n>/prompt.md` to see the latest prompt the worker sent.
5. Read `.a-dev/runs/issue-<n>/codex.log`, `verify.log`, `review.md`, `summary.md`, and `pr_body.md` depending on the failure stage.

For a local markdown board:

1. Run `a-dev kanban` to refresh `.a-dev/kanban.md`.
2. Use `a-dev kanban --issue <n>` to render only one issue.
3. Use `a-dev kanban --output path/to/file.md` to write somewhere else.

For a parent issue run:

1. Open `.a-dev/runs/issue-<parent>/parent-plan.json` for the current sub-issue DAG snapshot.
2. Read `.a-dev/runs/issue-<parent>/parent-memory.md` for accumulated child PR summaries and decisions.
3. Inspect each child issue directory for its normal `latest.json`, `summary.md`, `verify.log`, and PR body.

For daemon state:

1. Run `a-dev status`.
2. Read `.a-dev/runtime/worker.status.json`.
3. Read `.a-dev/logs/worker.log`.
4. Check `.a-dev/runtime/worker.lock` and `.a-dev/runtime/worker.pid`.

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
- Before each review pass, untracked files are marked with git intent-to-add so review sees new file contents in `git diff HEAD`; final commit staging still happens later.
- Stacked PRs are only considered when dependency checking is enabled, there is exactly one open blocker, and that blocker already has a recorded `pr_opened` job.
- Parent issues carry `ai-parent` and orchestrate `ai-child` sub-issues. Children remain the code-producing PR units.
- Parent runs process children serially up to `issue_selection.max_parent_children_per_run` and leave the parent `ai-ready` if blocked or only partially drained.
- `merge` is an explicit operator action, not part of the daemon loop. It assumes CI/verifier confidence was established locally before the command is run.
- `reset` is a destructive debugging action for rerunning an issue from scratch. It removes local run state and closes recorded PRs instead of preserving resume context.
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
