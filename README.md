# Local AI Issue Worker

`local-ai-issue-worker` is a local CLI that processes GitHub issues labeled for AI work. It uses `gh` for GitHub operations, `git worktree` for isolated changes, a configurable Codex CLI backend for edits, local verifier commands, and draft pull requests for human review. It can also resume work on an existing ai-issue PR with follow-up instructions from local operator notes, issue comments, and PR discussion, either immediately or through the normal background queue.

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
ai-issue init
```

`init` writes `.ai-issue-worker.yaml`, inferring the GitHub repo from
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
directories `.ai-logs`, `.ai-runs`, `.ai-runtime`, and `.ai-worktrees` to
`.gitignore`.

List candidate issues:

```bash
ai-issue list
```

By default, candidates exclude issues that have open native GitHub issue
dependencies in their `blocked by` relationship, in addition to excluding
configured blocked labels such as `blocked` and `needs-human`.

To let the worker continue through a dependency chain, enable stacked PRs. In
this mode, an issue with exactly one open blocker can be selected after that
blocker has an ai-issue PR open; the downstream worktree is based on the
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
ai-issue create --title "Fix parser crash" "Parser crashes when input is empty."
```

By default, `--mode auto` lets Codex decide between one issue and a parent issue
with sub-issues. Use `--mode single` to force one ready issue, or `--mode parent`
to force an `ai-ready` + `ai-parent` tracking issue with `ai-child` sub-issues
and native GitHub `blocked_by` dependency edges. Parent plans are edited as JSON;
single issue drafts keep the `Title:` plus Markdown editor format.

It uses the same `agent.command`, `agent.model`, and `agent.reasoning` settings
as the worker. Use `--description-file path/to/issue.txt` for longer notes, or
`--no-edit` for non-interactive scripts.

When `run-once` selects a parent issue, it processes eligible child issues
serially in separate Codex sessions and child PRs, up to
`issue_selection.max_parent_children_per_run` per run. Parent runs write
`parent-plan.json` and `parent-memory.md` under `.ai-runs/issue-<parent>/` so
later child prompts include prior child summaries and decisions. Downstream child
issues only run before blockers close when `issue_selection.allow_stacked_prs`
allows the existing stacked-PR behavior.

Run one local cycle:

```bash
ai-issue run-once
```

Pick a Codex model and reasoning effort for a single run:

```bash
ai-issue run-once --model gpt-5.4 --reasoning high
```

For persistent defaults, set these in `.ai-issue-worker.yaml`:

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
read-only Codex sandbox. If that review reports configured blocking priorities,
the worker runs a separate Codex fix session, verifies again, and repeats until
the review is clean or `review.max_iterations` fix passes have been used.

Each issue run directory also contains `artifacts.log`, a timestamped manifest of
generated run artifacts such as prompts, Codex logs, verifier logs, review files,
job records, PR bodies, resume summaries, and latest-file updates. After each Codex session, the
manifest records token usage when the configured Codex command exposes it in
stdout/stderr, plus a cumulative total across Codex logs in that issue directory.

Start a simple background loop:

```bash
ai-issue start
ai-issue status
ai-issue logs
ai-issue stop
```

Resume work on an existing ai-issue PR for a specific issue. The worker reuses the recorded branch/worktree for that issue, includes the latest local `summary.md` artifact plus new issue comments and PR review discussion since the last worker run, accepts an optional local operator note, and updates the existing PR instead of opening a new one:

```bash
ai-issue resume 123 --comment "Address the latest review feedback and keep the API unchanged."
```

Queue that same follow-up work for the normal `run-once` / `start` scheduler path instead of running it immediately:

```bash
ai-issue resume 123 --queue --comment "Address the latest review feedback and keep the API unchanged."
```

Queued resume work is represented by the `ai-resume` label. The command above also posts the optional note as a GitHub issue comment so a later background run can include it in the continuation prompt.

## Safety

V1 is not sandboxed. Run it only on trusted repositories and keep draft PR review enabled. The worker does not auto-merge.

GitHub issue comments, issue bodies, and PR bodies are scrubbed before upload to
mask local user-home paths such as `/Users/name/...`.
