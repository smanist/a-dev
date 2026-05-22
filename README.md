# A-Dev

`a-dev` is a local CLI that processes GitHub issues labeled for AI work. It uses `gh` for GitHub operations, `git worktree` for isolated changes, a configurable Codex CLI backend for edits, local verifier commands, and draft pull requests for human review. It can also resume work on an existing A-Dev PR with follow-up instructions from local operator notes, issue comments, and PR discussion, either immediately or through the normal background queue.

## Install

```bash
python -m pip install -e .
```

For tests:

```bash
python -m pip install -e '.[test]'
pytest
```

## Quick Start

```bash
a-dev init
```

`init` writes `.a-dev.yaml`, inferring the GitHub repo from
`origin` and the base branch from `origin/HEAD` or the current branch when
possible:

```yaml
repo: owner/repo
base_branch: main
```

It also creates or updates the GitHub labels used by the automation, including
`ai-ready`, `ai-resume`, `ai-working`, `ai-failed`, and `ai-pr-opened`, when `gh` is
authenticated for the inferred repo. Use `--repo`, `--base-branch`, or
`--no-create-labels` to override those defaults. It appends the local artifact
directory `.a-dev/` to `.gitignore`, and creates `.vscode/settings.json` plus
`.vscode/tasks.json` from the bundled templates. Existing VS Code files are
preserved unless `--force` is used.

List candidate issues:

```bash
a-dev list
```

List open issues, all issues, open pull requests, or all pull requests:

```bash
a-dev list --open-issues
a-dev list --all-issues
a-dev list --open-prs
a-dev list --prs
```

By default, candidates exclude issues that have open native GitHub issue
dependencies in their `blocked by` relationship, in addition to excluding
configured blocked labels such as `blocked` and `needs-human`.

To let the worker continue through a dependency chain, enable stacked PRs. In
this mode, an issue with exactly one open blocker can be selected after that
blocker has an A-Dev PR open; the downstream worktree is based on the
blocker's branch and its PR targets that branch:

```yaml
issue_selection:
  allow_stacked_prs: true
  max_stack_depth: 3
```

To use label-only selection, disable the dependency check:

```yaml
issue_selection:
  respect_issue_dependencies: false
```

Create new AI-ready issue work from rough local notes. The command sends your
notes through the configured Codex agent to draft formal issue content, opens
that draft in your editor, then creates GitHub issue records:

```bash
a-dev create --title "Fix parser crash" "Parser crashes when input is empty."
```

Longer notes can carry their own title in a leading `Title:` line:

```markdown
Title: Fix parser crash

Parser crashes when input is empty.
```

Then create from the file directly:

```bash
a-dev create --description-file issue.md
```

To write temporary notes without saving a scratch file first, use:

```bash
a-dev create --input-editor
```

That opens a temporary note with the same leading `Title:` format, then deletes
the temporary file after reading it.

By default, `--mode auto` lets Codex decide between one issue and a parent issue
with sub-issues. Use `--mode single` to force one ready issue, or `--mode parent`
to force an `ai-ready` + `ai-parent` tracking issue with `ai-child` sub-issues
and native GitHub `blocked_by` dependency edges. Parent plans are edited as JSON;
single issue drafts keep the `Title:` plus Markdown editor format.

It uses the same `agent.command`, `agent.model`, and `agent.reasoning` settings
as the worker. Use `--no-edit` for non-interactive scripts. If no `--editor`,
`VISUAL`, or `EDITOR` is set, the generated draft opens with `code --wait` when
VS Code's command-line launcher is available.

When `run-once` selects a parent issue, it processes eligible child issues
serially in separate Codex sessions and child PRs, up to
`issue_selection.max_parent_children_per_run` per run. Parent runs write
`parent-plan.json` and `parent-memory.md` under `.a-dev/runs/issue-<parent>/` so
later child prompts include prior child summaries and decisions. Downstream child
issues only run before blockers close when `issue_selection.allow_stacked_prs`
allows the existing stacked-PR behavior.

If all remaining child issues are dependency-blocked, the parent is marked
`ai-parent-blocked` and `ai-ready` is removed so the scheduler does not spin on
it. `a-dev merge` re-enables affected paused parents when a merged child leaves
another child runnable. If one child fails, the parent continues with any other
runnable children and only receives `ai-failed` when no runnable child work
remains.

Run one local cycle:

```bash
a-dev run-once
```

Pick a Codex model and reasoning effort for a single run:

```bash
a-dev run-once --model gpt-5.4 --reasoning high
```

For persistent defaults, set these in `.a-dev.yaml`:

```yaml
agent:
  command: codex exec --full-auto
  model: gpt-5.4
  reasoning: high

review:
  enabled: true
  command: codex exec --sandbox read-only
  max_iterations: 3
  fix_priorities: [P0, P1]
```

When review is enabled, the worker runs a separate Codex code-review session after
the initial implementation and verifier pass. The review command defaults to a
read-only Codex sandbox. Before each review pass, the worker marks untracked
files with git intent-to-add so review diffs include new file contents. If that
review reports configured blocking priorities, the worker runs a separate Codex
fix session, verifies again, and repeats until the review is clean or
`review.max_iterations` fix passes have been used.

Each issue run directory also contains `artifacts.log`, a timestamped manifest of
generated run artifacts such as prompts, Codex logs, verifier logs, review files,
job records, PR bodies, resume summaries, and latest-file updates. After each Codex session, the
manifest records token usage when the configured Codex command exposes it in
stdout/stderr, plus a cumulative total across Codex logs in that issue directory.

Generate an Obsidian-friendly kanban checklist from local run artifacts:

```bash
a-dev kanban
```

The command writes `.a-dev/kanban.md` and prints the same Markdown. It creates
one checkbox row per latest issue run, marks rows checked after a successful
commit/push/PR-open status, summarizes the net `summary.md` contents when
available, and always includes token usage plus PR status bullets. Use
`--issue <number>` to render one issue or `--output path/to/file.md` to choose a
different destination.

Start a simple background loop:

```bash
a-dev start
a-dev status
a-dev logs
a-dev stop
```

`a-dev status` includes daemon state, the worker lock, the latest job phase, any
unfinished `working` job, open `ai-working` issues, and interruption diagnostics.
Pass `--json` for scheduler-friendly machine-readable output.

Temporarily remove an issue from scheduler selection:

```bash
a-dev disable 123
```

Enable or re-enable an issue for worker selection. This adds `ai-ready` and
removes `ai-failed`, replacing the old retry flow. It leaves blocked labels such
as `blocked` and `needs-human` in place so dependency constraints remain
separate:

```bash
a-dev enable 123
```

Reset an issue for a fresh worker run during debugging:

```bash
a-dev reset 123
```

`reset` closes recorded A-Dev PRs for the issue, asks GitHub to delete the PR
branches, removes local worker worktrees and branches, removes the issue run
directory, clears A-Dev lifecycle labels such as `ai-working`, `ai-failed`,
`ai-pr-opened`, and `ai-resume`, then adds `ai-ready`. GitHub does not support
deleting PR records, so reset closes them instead. Use `--dry-run` to preview
the cleanup.

Resume work on an existing A-Dev PR for a specific issue. The worker reuses the recorded branch/worktree for that issue, includes the latest local `summary.md` artifact plus new issue comments and PR review discussion since the last worker run, accepts an optional local operator note, and updates the existing PR instead of opening a new one:

```bash
a-dev resume 123 --comment "Address the latest review feedback and keep the API unchanged."
```

Queue that same follow-up work for the normal `run-once` / `start` scheduler path instead of running it immediately:

```bash
a-dev resume 123 --queue --comment "Address the latest review feedback and keep the API unchanged."
```

Queued resume work is represented by the `ai-resume` label. The command above also posts the optional note as a GitHub issue comment so a later background run can include it in the continuation prompt.

Check out the active A-Dev PR branch for an issue from local run state:

```bash
a-dev checkout 123
```

`checkout` only acts when the latest local run for the issue has an active
A-Dev PR. If the worker worktree is still present, it prints that path instead
of switching the current checkout. If there is no PR, or the latest run failed,
it leaves the checkout unchanged and prints the current run status.

After reviewing an opened PR locally, committing any manual edits, and pushing
the branch, merge it explicitly from local worker state:

```bash
a-dev merge 123 --method merge
```

`merge` finds the recorded PR for issue `123`, verifies the local branch is clean
and matches `origin/<branch>` when that branch exists locally, asks GitHub to
merge the PR, removes A-Dev PR/resume labels, records a `pr_merged` job, and
deletes the local and remote branch. After an immediate merge, it fast-forward
pulls the base branch when the local checkout is on that branch. Use `--method
squash` or `--method rebase` for those GitHub merge modes, `--auto` to enable
GitHub auto-merge when branch protection requirements are still pending,
`--admin` to use administrator privileges, `--ready` to mark a draft PR ready
before merging, `--dry-run` to preview, or `--keep-branch` to leave the branch in
place. Auto-merge records `pr_auto_merge_enabled` and keeps the branch because
the PR has not merged yet.

## Safety

V1 is not sandboxed. Run it only on trusted repositories and keep draft PR review enabled. The worker does not auto-merge during background issue processing; merging requires an explicit `a-dev merge` operator command.

GitHub issue comments, issue bodies, and PR bodies are scrubbed before upload to
mask local user-home paths such as `/Users/name/...`.
