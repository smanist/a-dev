from __future__ import annotations

import argparse
import json
import os
import signal
import time
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, REASONING_EFFORTS, load_config
from .jobs import utc_iso
from .runner import RunOverrides, configured_paths, run_once


STOP = False


def _handle_stop(signum, frame) -> None:  # noqa: ARG001
    global STOP
    STOP = True


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def write_status(path: Path, **values) -> None:
    existing = read_json(path)
    existing.update(values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def daemon_loop(
    config_path: Path,
    interval_minutes: int | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> int:
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    config = load_config(config_path)
    root = Path.cwd()
    paths = configured_paths(config, root)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    pid_file = paths["runtime_root"] / "worker.pid"
    status_file = paths["runtime_root"] / "worker.status.json"
    log_file = paths["log_root"] / "worker.log"
    interval = (interval_minutes or config.scheduler.interval_minutes) * 60

    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    write_status(
        status_file,
        running=True,
        pid=os.getpid(),
        started_at=utc_iso(),
        last_run_at=None,
        last_status=None,
        last_error=None,
        log_file=str(log_file),
    )
    try:
        while not STOP:
            exit_code = run_once(
                config_path,
                repo_root=root,
                overrides=RunOverrides(model=model, reasoning=reasoning),
            )
            write_status(
                status_file,
                running=True,
                pid=os.getpid(),
                last_run_at=utc_iso(),
                last_status=f"exit_{exit_code}",
                last_error=None if exit_code == 0 else f"run-once exited {exit_code}",
            )
            slept = 0
            while slept < interval and not STOP:
                time.sleep(min(1, interval - slept))
                slept += 1
    finally:
        write_status(status_file, running=False, pid=os.getpid())
        if pid_file.exists():
            pid_file.unlink()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--interval", type=int)
    parser.add_argument("--model")
    parser.add_argument("--reasoning", choices=REASONING_EFFORTS)
    args = parser.parse_args(argv)
    return daemon_loop(Path(args.config), args.interval, model=args.model, reasoning=args.reasoning)


if __name__ == "__main__":
    raise SystemExit(main())
