from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from .models import JobRecord
from .token_usage import (
    TokenUsage,
    format_token_usage,
    parse_token_usage,
    sum_token_usages,
)


ARTIFACT_LOG_NAME = "artifacts.log"


def utc_timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def utc_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def local_log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def issue_run_dir(run_root: Path, issue_number: int) -> Path:
    return run_root / f"issue-{issue_number}"


def append_artifact_log(run_dir: Path, message: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / ARTIFACT_LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(f"{local_log_timestamp()} - {message}\n")


def record_artifact_file(path: Path, action: str = "wrote") -> None:
    append_artifact_log(path.parent, f"{action} {path.name}")


def copy_artifact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    append_artifact_log(
        destination.parent, f"updated {destination.name} from {source.name}"
    )


def write_single_artifact(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    record_artifact_file(path)


def write_text_artifact(path: Path, latest_path: Path, content: str) -> None:
    write_single_artifact(path, content)
    copy_artifact(path, latest_path)


def write_job_record(
    run_dir: Path, record: JobRecord, timestamp: str | None = None
) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or utc_timestamp()
    path = run_dir / f"run-{stamp}.json"
    path.write_text(
        json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    record_artifact_file(path)
    copy_artifact(path, run_dir / "latest.json")
    return path


def _codex_log_usages(run_dir: Path) -> tuple[list[TokenUsage], int]:
    usages: list[TokenUsage] = []
    seen = 0
    for log_path in sorted(run_dir.glob("codex-*.log")):
        seen += 1
        try:
            usage = parse_token_usage(
                log_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            usage = None
        if usage:
            usages.append(usage)
    return usages, seen


def record_codex_token_usage(run_dir: Path, log_path: Path) -> None:
    try:
        usage = parse_token_usage(
            log_path.read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        usage = None
    append_artifact_log(run_dir, f"tokens {log_path.name}: {format_token_usage(usage)}")

    usages, seen = _codex_log_usages(run_dir)
    total = sum_token_usages(usages)
    if total:
        append_artifact_log(
            run_dir,
            f"tokens total: {format_token_usage(total)} across {len(usages)}/{seen} codex run(s)",
        )
    else:
        append_artifact_log(
            run_dir, f"tokens total: unavailable across {seen} codex run(s)"
        )


def load_job_record(path: Path) -> JobRecord:
    return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))


def recent_jobs(run_root: Path) -> list[JobRecord]:
    records: list[JobRecord] = []
    if not run_root.exists():
        return records
    for latest in sorted(run_root.glob("issue-*/latest.json")):
        try:
            records.append(load_job_record(latest))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    records.sort(key=lambda record: record.started_at, reverse=True)
    return records
