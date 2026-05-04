from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import (
    DEFAULT_CONFIG_PATH,
    REASONING_EFFORTS,
    ConfigError,
    load_config,
    write_default_config,
)
from .codex_backend import CodexBackend
from .daemon import pid_alive, read_json, write_status
from .github_gh import GHClient, GHError
from .jobs import issue_run_dir, recent_jobs
from .locking import lock_status
from .prompt import build_issue_draft_prompt
from .runner import (
    RunOverrides,
    configured_paths,
    resume_issue,
    run_once,
    workable_issues,
)
from .shell import run_cmd
from .worktree import GitError, remove_worktree


AI_ISSUE_GITIGNORE_ENTRIES = [".ai-logs", ".ai-runs", ".ai-runtime", ".ai-worktrees"]
CREATE_MODES = ("auto", "single", "parent")


@dataclass(frozen=True)
class IssueDraft:
    title: str
    body: str


@dataclass(frozen=True)
class ChildIssueDraft(IssueDraft):
    key: str
    blocked_by: list[str]


@dataclass(frozen=True)
class IssueDraftPlan:
    kind: str
    issue: IssueDraft | None = None
    parent: IssueDraft | None = None
    children: list[ChildIssueDraft] | None = None


def parse_interval_minutes(value: str) -> int:
    raw = value.strip().lower()
    if raw.endswith("m"):
        return int(raw[:-1])
    if raw.endswith("h"):
        return int(raw[:-1]) * 60
    return int(raw)


def parse_age(value: str) -> timedelta:
    raw = value.strip().lower()
    if raw.endswith("d"):
        return timedelta(days=int(raw[:-1]))
    if raw.endswith("h"):
        return timedelta(hours=int(raw[:-1]))
    return timedelta(days=int(raw))


def _parse_started_at(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.removesuffix("Z"))
    except ValueError:
        return None


def _load(path: str):
    return load_config(Path(path))


def _paths(config, root: Path):
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _repo_from_remote_url(url: str) -> str | None:
    raw = url.strip()
    if not raw:
        return None
    if raw.endswith(".git"):
        raw = raw[:-4]
    if "://" not in raw and ":" in raw and "@" in raw:
        host, path = raw.split("@", 1)[1].split(":", 1)
        parts = [part for part in path.split("/") if part]
    else:
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return None
        host = parsed.hostname or parsed.netloc
        parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[-2], parts[-1]
    if not owner or not repo:
        return None
    if host in {"github.com", "www.github.com"}:
        return f"{owner}/{repo}"
    return f"{host}/{owner}/{repo}"


def _git_stdout(args: list[str]) -> str | None:
    result = run_cmd(args)
    if result.exit_code != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _infer_repo_from_git() -> str | None:
    remote_url = _git_stdout(["git", "remote", "get-url", "origin"])
    if not remote_url:
        return None
    return _repo_from_remote_url(remote_url)


def _infer_base_branch_from_git() -> str | None:
    origin_head = _git_stdout(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"]
    )
    if origin_head:
        return origin_head.removeprefix("origin/")
    current = _git_stdout(["git", "branch", "--show-current"])
    if current:
        return current
    default_branch = _git_stdout(["git", "config", "--get", "init.defaultBranch"])
    return default_branch


def _automation_label_specs(config) -> dict[str, tuple[str, str]]:
    specs: dict[str, tuple[str, str]] = {}

    def add(name: str, color: str, description: str) -> None:
        if name and name not in specs:
            specs[name] = (color, description)

    labels = config.issue_selection
    add(labels.ready_label, "0E8A16", "Ready for the local AI issue worker to process.")
    add(
        labels.resume_label,
        "FB8C00",
        "Queue a follow-up pass on an existing AI issue worker pull request.",
    )
    add(
        labels.working_label,
        "1D76DB",
        "Currently being processed by the local AI issue worker.",
    )
    add(
        labels.failed_label,
        "D73A4A",
        "The local AI issue worker failed to complete this issue.",
    )
    add(
        labels.pr_opened_label,
        "5319E7",
        "The local AI issue worker opened a pull request for this issue.",
    )
    add(
        labels.parent_label,
        "6F42C1",
        "Parent tracking issue for local AI issue worker sub-issue orchestration.",
    )
    add(
        labels.child_label,
        "0969DA",
        "Sub-issue owned by a local AI issue worker parent issue.",
    )
    add(
        labels.parent_done_label,
        "8250DF",
        "Parent issue orchestration has opened all available child pull requests.",
    )
    for label in labels.blocked_labels:
        add(
            label,
            "FBCA04",
            "Blocked from local AI issue worker selection until human action is taken.",
        )
    return specs


def _ensure_gitignore_entries(path: Path = Path(".gitignore")) -> None:
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = {line.strip() for line in existing}
    missing = [entry for entry in AI_ISSUE_GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return

    output = "\n".join(existing).rstrip()
    if output:
        output += "\n\n"
    output += "# Local AI Issue Worker artifacts\n" + "\n".join(missing) + "\n"
    path.write_text(output, encoding="utf-8")


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped
    return ""


def _derive_issue_title(description: str, title: str | None) -> str:
    if title:
        return title.strip()
    first = _first_nonempty_line(description)
    if not first:
        return ""
    first = first.removeprefix("- ").removeprefix("* ").strip()
    return first[:120].rstrip(" .")


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def _draft_item(data: Any, context: str) -> IssueDraft:
    if not isinstance(data, dict):
        raise RuntimeError(f"{context} must be an object")
    title = data.get("title")
    body = data.get("body")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeError(f"{context} title is empty")
    if not isinstance(body, str) or not body.strip():
        raise RuntimeError(f"{context} body is empty")
    return IssueDraft(title.strip(), body.strip())


def _child_draft_item(data: Any) -> ChildIssueDraft:
    draft = _draft_item(data, "child issue")
    key = data.get("key") if isinstance(data, dict) else None
    blocked_by = data.get("blocked_by", []) if isinstance(data, dict) else []
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("child issue key is empty")
    if not isinstance(blocked_by, list) or any(
        not isinstance(item, str) for item in blocked_by
    ):
        raise RuntimeError(
            f"child issue `{key}` blocked_by must be a list of child keys"
        )
    return ChildIssueDraft(
        draft.title,
        draft.body,
        key.strip(),
        [item.strip() for item in blocked_by if item.strip()],
    )


def _validate_acyclic_children(children: list[ChildIssueDraft]) -> None:
    keys = [child.key for child in children]
    if len(set(keys)) != len(keys):
        raise RuntimeError("parent issue children must have unique keys")
    key_set = set(keys)
    for child in children:
        missing = [key for key in child.blocked_by if key not in key_set]
        if missing:
            raise RuntimeError(
                f"child issue `{child.key}` references unknown blocked_by key(s): {', '.join(missing)}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    edges = {child.key: child.blocked_by for child in children}

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise RuntimeError("parent issue dependency graph must be acyclic")
        visiting.add(key)
        for blocker in edges[key]:
            visit(blocker)
        visiting.remove(key)
        visited.add(key)

    for key in keys:
        visit(key)


def _validate_draft_plan(plan: IssueDraftPlan, requested_mode: str) -> IssueDraftPlan:
    if plan.kind not in {"single", "parent"}:
        raise RuntimeError("issue draft kind must be `single` or `parent`")
    if requested_mode == "single" and plan.kind != "single":
        raise RuntimeError(
            "agent returned a parent issue plan while --mode single was requested"
        )
    if requested_mode == "parent" and plan.kind != "parent":
        raise RuntimeError(
            "agent returned a single issue plan while --mode parent was requested"
        )
    if plan.kind == "single":
        if plan.issue is None:
            raise RuntimeError("single issue draft is missing issue content")
        return plan
    children = plan.children or []
    if plan.parent is None:
        raise RuntimeError("parent issue draft is missing parent content")
    if not children:
        raise RuntimeError("parent issue draft must contain at least one child issue")
    _validate_acyclic_children(children)
    return plan


def _parse_issue_draft_json(text: str, requested_mode: str = "auto") -> IssueDraftPlan:
    stripped = _strip_code_fence(text)
    candidates = [stripped]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("title"), str) and isinstance(data.get("body"), str):
            plan = IssueDraftPlan("single", issue=_draft_item(data, "issue"))
            return _validate_draft_plan(plan, requested_mode)
        kind = data.get("kind")
        if kind == "single":
            plan = IssueDraftPlan(
                "single", issue=_draft_item(data.get("issue"), "issue")
            )
            return _validate_draft_plan(plan, requested_mode)
        if kind == "parent":
            raw_children = data.get("children")
            if not isinstance(raw_children, list):
                raise RuntimeError("parent issue draft children must be a list")
            plan = IssueDraftPlan(
                "parent",
                parent=_draft_item(data.get("parent"), "parent issue"),
                children=[_child_draft_item(item) for item in raw_children],
            )
            return _validate_draft_plan(plan, requested_mode)
    raise RuntimeError("agent did not return valid issue draft JSON")


def _render_issue_plan_json(plan: IssueDraftPlan) -> str:
    return json.dumps(asdict(plan), indent=2, sort_keys=False) + "\n"


def _render_issue_draft(title: str, body: str) -> str:
    body = body.strip()
    return f"""Title: {title.strip()}

{body}
"""


def _parse_issue_draft_file(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    title = ""
    body_start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--"):
            continue
        if stripped.lower().startswith("title:"):
            title = stripped.split(":", 1)[1].strip()
            body_start = index + 1
            break
        raise ConfigError("draft must start with a `Title:` line")
    body = "\n".join(lines[body_start:]).strip()
    if not title:
        raise ConfigError("issue title is empty after editing; aborting")
    if not body:
        raise ConfigError("issue body is empty after editing; aborting")
    return title, body


def _body_file(path: Path, body: str) -> Path:
    path.write_text(f"{body.strip()}\n", encoding="utf-8")
    return path


def _create_single_issue(
    config, gh: GHClient, draft_file: Path, plan: IssueDraftPlan, args
) -> str:
    assert plan.issue is not None
    draft_file.write_text(
        _render_issue_draft(plan.issue.title, plan.issue.body), encoding="utf-8"
    )
    if not args.no_edit:
        _run_editor(draft_file, args.editor)
    edited_title, edited_body = _parse_issue_draft_file(
        draft_file.read_text(encoding="utf-8")
    )
    draft_file.write_text(f"{edited_body}\n", encoding="utf-8")
    return gh.create_issue(
        edited_title,
        draft_file,
        labels=[config.issue_selection.ready_label],
    )


def _create_parent_issue(
    config, gh: GHClient, draft_file: Path, draft_dir: Path, plan: IssueDraftPlan, args
) -> str:
    draft_file.write_text(_render_issue_plan_json(plan), encoding="utf-8")
    if not args.no_edit:
        _run_editor(draft_file, args.editor)
    edited_plan = _parse_issue_draft_json(
        draft_file.read_text(encoding="utf-8"), requested_mode="parent"
    )
    assert edited_plan.parent is not None
    assert edited_plan.children is not None

    parent_body = _body_file(draft_dir / "parent.md", edited_plan.parent.body)
    parent = gh.create_issue_record(
        edited_plan.parent.title,
        parent_body,
        labels=[
            config.issue_selection.ready_label,
            config.issue_selection.parent_label,
        ],
    )

    created_children = {}
    for child in edited_plan.children:
        child_body = _body_file(draft_dir / f"child-{child.key}.md", child.body)
        created = gh.create_issue_record(
            child.title,
            child_body,
            labels=[config.issue_selection.child_label],
        )
        created_children[child.key] = created
        gh.add_sub_issue(parent.number, created.id)

    for child in edited_plan.children:
        created = created_children[child.key]
        for blocker_key in child.blocked_by:
            gh.add_blocked_by(created.number, created_children[blocker_key].id)

    return parent.url


def _read_description(args) -> str:
    if args.description_file:
        if args.description_file == "-":
            return sys.stdin.read()
        return Path(args.description_file).read_text(encoding="utf-8")
    if args.description:
        return " ".join(args.description)
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _read_resume_comment(args) -> str:
    if args.comment_file:
        if args.comment_file == "-":
            return sys.stdin.read()
        return Path(args.comment_file).read_text(encoding="utf-8")
    if args.comment:
        return args.comment
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def _editor_command(editor: str | None = None) -> list[str]:
    for command in [editor, os.environ.get("VISUAL"), os.environ.get("EDITOR")]:
        if command:
            return shlex.split(command)
    for name in ["nano", "vi"]:
        path = shutil.which(name)
        if path:
            return [path]
    raise RuntimeError("no editor found; set EDITOR or pass --no-edit")


def _run_editor(path: Path, editor: str | None = None) -> None:
    result = subprocess.run([*_editor_command(editor), str(path)], check=False)
    if result.returncode != 0:
        raise RuntimeError(f"editor exited with status {result.returncode}")


def _generate_issue_draft(
    config,
    repo_root: Path,
    description: str,
    title_hint: str,
    draft_dir: Path,
    mode: str = "auto",
) -> IssueDraftPlan:
    prompt_path = draft_dir / "issue-draft.prompt.md"
    log_path = draft_dir / "issue-draft.log"
    prompt_path.write_text(
        build_issue_draft_prompt(
            description, repo_root, config, title_hint=title_hint, mode=mode
        ),
        encoding="utf-8",
    )
    backend = CodexBackend(
        config.agent.command,
        log_path=log_path,
        model=config.agent.model,
        reasoning=config.agent.reasoning,
    )
    result = backend.run(
        repo_root, prompt_path, timeout_sec=config.agent.timeout_minutes * 60
    )
    if not result.success:
        detail = (
            result.stderr.strip()
            or result.stdout.strip()
            or "agent failed without output"
        )
        raise RuntimeError(f"issue draft generation failed: {detail}")
    plan = _parse_issue_draft_json(result.stdout, requested_mode=mode)
    if plan.kind == "single" and plan.issue and not plan.issue.title:
        plan = IssueDraftPlan(
            "single", issue=IssueDraft(title_hint.strip(), plan.issue.body)
        )
    if plan.kind == "single" and (plan.issue is None or not plan.issue.title):
        raise RuntimeError("agent returned an empty issue title")
    return plan


def cmd_init(args) -> int:
    repo = args.repo or _infer_repo_from_git() or "owner/repo"
    base_branch = args.base_branch or _infer_base_branch_from_git() or "main"
    path = Path(args.path)
    try:
        write_default_config(path, force=args.force, repo=repo, base_branch=base_branch)
        _ensure_gitignore_entries()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"failed to update .gitignore: {exc}", file=sys.stderr)
        return 1
    print(f"created {args.path}")
    if repo == "owner/repo":
        print(
            "repo could not be inferred; edit repo in the config before running the worker",
            file=sys.stderr,
        )
        return 0
    if args.no_create_labels:
        return 0
    try:
        config = load_config(path)
        GHClient(config.repo).ensure_labels(_automation_label_specs(config))
    except (ConfigError, GHError) as exc:
        print(f"warning: could not create GitHub labels: {exc}", file=sys.stderr)
        return 0
    print("created or updated GitHub labels")
    return 0


def cmd_list(args) -> int:
    try:
        config = _load(args.config)
        gh = GHClient(config.repo)
        issues = gh.list_issues(
            [config.issue_selection.ready_label, config.issue_selection.resume_label]
        )
        candidates = workable_issues(
            gh,
            issues,
            config.issue_selection,
            config.base_branch,
            _paths(config, Path.cwd()),
        )
    except (ConfigError, GHError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                [issue.__dict__ for issue in candidates], indent=2, sort_keys=True
            )
        )
        return 0
    for issue in candidates:
        print(
            f"#{issue.number}\t{issue.state}\t{issue.updated_at or ''}\t{','.join(issue.labels)}\t{issue.title}"
        )
    return 0


def cmd_create(args) -> int:
    try:
        config = _load(args.config)
        description = _read_description(args).strip()
        if not description:
            raise ConfigError(
                "issue description is required; pass text, --description-file, or pipe stdin"
            )
        title_hint = _derive_issue_title(description, args.title)
        draft_dir = Path(tempfile.mkdtemp(prefix="ai-issue-create-"))
        draft_file = draft_dir / (
            "issue-plan.json" if args.mode == "parent" else "issue.md"
        )
        try:
            plan = _generate_issue_draft(
                config, Path.cwd(), description, title_hint, draft_dir, mode=args.mode
            )
            gh = GHClient(config.repo)
            if plan.kind == "single":
                url = _create_single_issue(config, gh, draft_file, plan, args)
            else:
                draft_file = draft_dir / "issue-plan.json"
                url = _create_parent_issue(
                    config, gh, draft_file, draft_dir, plan, args
                )
        finally:
            shutil.rmtree(draft_dir, ignore_errors=True)
    except (ConfigError, GHError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"created issue: {url}")
    return 0


def cmd_run_once(args) -> int:
    return run_once(
        Path(args.config),
        overrides=RunOverrides(model=args.model, reasoning=args.reasoning),
    )


def cmd_inspect(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    jobs = recent_jobs(paths["run_root"])
    data = {
        "repo": config.repo,
        "base_branch": config.base_branch,
        "labels": config.issue_selection.__dict__,
        "lock_status": lock_status(paths["runtime_root"] / "worker.lock"),
        "worktree_root": str(paths["worktree_root"]),
        "run_root": str(paths["run_root"]),
        "jobs": [
            job.to_dict()
            for job in jobs
            if args.issue is None or job.issue_number == args.issue
        ],
        "open_working_issues": [],
    }
    try:
        working = GHClient(config.repo).list_issues(
            config.issue_selection.working_label
        )
        data["open_working_issues"] = [issue.__dict__ for issue in working]
    except GHError:
        pass
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        print(f"repo: {data['repo']}")
        print(f"base_branch: {data['base_branch']}")
        print(f"lock: {data['lock_status']}")
        print(f"worktree_root: {data['worktree_root']}")
        print(f"run_root: {data['run_root']}")
        print("recent jobs:")
        for job in data["jobs"]:
            print(
                f"  issue #{job['issue_number']}: {job['status']} {job['branch_name']}"
            )
        if data["open_working_issues"]:
            print("open ai-working issues:")
            for issue in data["open_working_issues"]:
                print(f"  #{issue['number']}: {issue['title']}")
    return 0


def cmd_start(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    status_file = paths["runtime_root"] / "worker.status.json"
    log_file = paths["log_root"] / "worker.log"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            if pid_alive(pid):
                print(f"worker already running with pid {pid}")
                return 1
        except ValueError:
            pass
    interval = (
        args.interval_minutes
        if args.interval_minutes is not None
        else config.scheduler.interval_minutes
    )
    if args.foreground:
        from .daemon import daemon_loop

        return daemon_loop(
            Path(args.config), interval, model=args.model, reasoning=args.reasoning
        )
    log_handle = log_file.open("a", encoding="utf-8")
    command = [
        sys.executable,
        "-m",
        "ai_issue_worker.daemon",
        "--config",
        str(Path(args.config).resolve()),
        "--interval",
        str(interval),
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.reasoning:
        command.extend(["--reasoning", args.reasoning])
    proc = subprocess.Popen(
        command,
        cwd=Path.cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    write_status(
        status_file,
        running=True,
        pid=proc.pid,
        started_at=None,
        last_status="starting",
        log_file=str(log_file),
    )
    print(f"started worker pid {proc.pid}")
    return 0


def cmd_stop(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    if not pid_file.exists():
        print("worker is not running")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_file.unlink()
        print("removed invalid pid file")
        return 0
    if not pid_alive(pid):
        pid_file.unlink()
        print("worker is not running")
        return 0
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if not pid_alive(pid):
            if pid_file.exists():
                pid_file.unlink()
            print("worker stopped")
            return 0
        time.sleep(0.1)
    print(f"sent SIGTERM to pid {pid}; process is still exiting")
    return 0


def cmd_status(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    pid_file = paths["runtime_root"] / "worker.pid"
    status = read_json(paths["runtime_root"] / "worker.status.json")
    pid = None
    running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            running = pid_alive(pid)
        except ValueError:
            running = False
    print(f"running: {'yes' if running else 'no'}")
    print(f"pid: {pid or status.get('pid') or ''}")
    print(f"started_at: {status.get('started_at') or ''}")
    print(f"last_run_at: {status.get('last_run_at') or ''}")
    print(f"last_status: {status.get('last_status') or ''}")
    print(
        f"log_file: {status.get('log_file') or str(paths['log_root'] / 'worker.log')}"
    )
    return 0


def cmd_logs(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    if args.issue:
        log_file = paths["run_root"] / f"issue-{args.issue}" / "verify.log"
    else:
        log_file = paths["log_root"] / "worker.log"
    if not log_file.exists():
        print(f"log file not found: {log_file}")
        return 0
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-args.tail :]:
        print(line)
    if args.follow:
        with log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(0, os.SEEK_END)
            try:
                while True:
                    line = handle.readline()
                    if line:
                        print(line, end="")
                    else:
                        time.sleep(1)
            except KeyboardInterrupt:
                return 0
    return 0


def cmd_retry(args) -> int:
    try:
        config = _load(args.config)
        gh = GHClient(config.repo)
        gh.remove_label(args.issue, config.issue_selection.failed_label)
        gh.add_label(args.issue, config.issue_selection.ready_label)
    except (ConfigError, GHError) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.run_now:
        return run_once(
            Path(args.config),
            overrides=RunOverrides(model=args.model, reasoning=args.reasoning),
        )
    print(f"issue #{args.issue} marked ready")
    return 0


def cmd_resume(args) -> int:
    if args.queue:
        try:
            config = _load(args.config)
            gh = GHClient(config.repo)
            comment = _read_resume_comment(args).strip()
            if comment:
                run_dir = issue_run_dir(
                    _paths(config, Path.cwd())["run_root"], args.issue
                )
                comment_file = run_dir / "queued-resume-comment.md"
                comment_file.parent.mkdir(parents=True, exist_ok=True)
                comment_file.write_text(comment + "\n", encoding="utf-8")
                gh.comment(args.issue, comment_file)
            try:
                gh.remove_label(args.issue, config.issue_selection.failed_label)
            except GHError:
                pass
            gh.add_label(args.issue, config.issue_selection.resume_label)
        except (ConfigError, GHError) as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"issue #{args.issue} queued for resume")
        return 0
    comment = _read_resume_comment(args).strip()
    return resume_issue(
        Path(args.config),
        args.issue,
        manual_note=comment,
        overrides=RunOverrides(model=args.model, reasoning=args.reasoning),
    )


def cmd_clean(args) -> int:
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    paths = _paths(config, Path.cwd())
    jobs = recent_jobs(paths["run_root"])
    selected = [
        job for job in jobs if args.issue is None or job.issue_number == args.issue
    ]
    if args.failed:
        selected = [
            job
            for job in selected
            if "failed" in job.status
            or job.status in {"verify_failed", "agent_failed", "diff_rejected"}
        ]
    if args.older_than:
        cutoff = datetime.utcnow() - parse_age(args.older_than)
        selected = [
            job
            for job in selected
            if (started := _parse_started_at(job.started_at)) is not None
            and started < cutoff
        ]
    for job in selected:
        worktree = Path(job.worktree_path)
        print(f"{'would remove' if args.dry_run else 'removing'} worktree {worktree}")
        if not args.dry_run and worktree.exists():
            try:
                remove_worktree(worktree)
            except GitError as exc:
                print(f"failed to remove {worktree}: {exc}", file=sys.stderr)
                continue
        run_dir = issue_run_dir(paths["run_root"], job.issue_number)
        print(
            f"{'would remove' if args.dry_run else 'removing'} run directory {run_dir}"
        )
        if not args.dry_run and run_dir.exists():
            shutil.rmtree(run_dir)
        if args.delete_local_branches and not args.dry_run:
            run_cmd(["git", "branch", "-D", job.branch_name])
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-issue")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--path", default=DEFAULT_CONFIG_PATH)
    init.add_argument("--force", action="store_true")
    init.add_argument(
        "--repo", help="GitHub repository in owner/repo or host/owner/repo form"
    )
    init.add_argument("--base-branch", help="base branch for worker pull requests")
    init.add_argument(
        "--no-create-labels",
        action="store_true",
        help="skip creating worker labels on GitHub",
    )
    init.set_defaults(func=cmd_init)

    run = sub.add_parser("run-once")
    run.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    run.add_argument("--model")
    run.add_argument("--reasoning", choices=REASONING_EFFORTS)
    run.set_defaults(func=cmd_run_once)

    list_cmd = sub.add_parser("list")
    list_cmd.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    list_cmd.add_argument("--json", action="store_true")
    list_cmd.set_defaults(func=cmd_list)

    create = sub.add_parser("create")
    create.add_argument("description", nargs="*")
    create.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    create.add_argument("--title")
    create.add_argument("--description-file")
    create.add_argument("--editor")
    create.add_argument("--no-edit", action="store_true")
    create.add_argument("--mode", choices=CREATE_MODES, default="auto")
    create.set_defaults(func=cmd_create)

    inspect = sub.add_parser("inspect")
    inspect.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    inspect.add_argument("--issue", type=int)
    inspect.add_argument("--json", action="store_true")
    inspect.set_defaults(func=cmd_inspect)

    start = sub.add_parser("start")
    start.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    start.add_argument(
        "--interval", dest="interval_minutes", type=parse_interval_minutes
    )
    start.add_argument("--model")
    start.add_argument("--reasoning", choices=REASONING_EFFORTS)
    start.add_argument("--foreground", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = sub.add_parser("stop")
    stop.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    stop.set_defaults(func=cmd_stop)

    status = sub.add_parser("status")
    status.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    status.set_defaults(func=cmd_status)

    logs = sub.add_parser("logs")
    logs.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    logs.add_argument("--tail", type=int, default=100)
    logs.add_argument("--follow", action="store_true")
    logs.add_argument("--issue", type=int)
    logs.set_defaults(func=cmd_logs)

    retry = sub.add_parser("retry")
    retry.add_argument("issue", type=int)
    retry.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    retry.add_argument("--run-now", action="store_true")
    retry.add_argument("--model")
    retry.add_argument("--reasoning", choices=REASONING_EFFORTS)
    retry.set_defaults(func=cmd_retry)

    resume = sub.add_parser("resume")
    resume.add_argument("issue", type=int)
    resume.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    resume.add_argument("--queue", action="store_true")
    resume.add_argument("--comment")
    resume.add_argument("--comment-file")
    resume.add_argument("--model")
    resume.add_argument("--reasoning", choices=REASONING_EFFORTS)
    resume.set_defaults(func=cmd_resume)

    clean = sub.add_parser("clean")
    clean.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    clean.add_argument("--issue", type=int)
    clean.add_argument("--failed", action="store_true")
    clean.add_argument("--older-than")
    clean.add_argument("--dry-run", action="store_true")
    clean.add_argument("--delete-local-branches", action="store_true")
    clean.set_defaults(func=cmd_clean)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
