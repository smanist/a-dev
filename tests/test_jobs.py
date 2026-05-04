from pathlib import Path

from ai_issue_worker.jobs import load_job_record, write_job_record
from ai_issue_worker.jobs import (
    record_artifact_file,
    record_codex_token_usage,
    write_text_artifact,
)
from ai_issue_worker.models import JobRecord


def test_job_record_written_and_loaded(tmp_path: Path):
    record = JobRecord(
        issue_number=123,
        issue_title="Title",
        branch_name="ai/issue-123-title",
        worktree_path="/tmp/worktree",
        status="selected",
        started_at="2026-04-23T00:00:00Z",
        finished_at=None,
        pr_url=None,
        error_summary=None,
        changed_files=[],
        verifier_passed=None,
    )
    path = write_job_record(tmp_path, record, timestamp="20260423-000000")
    loaded = load_job_record(path)
    latest = load_job_record(tmp_path / "latest.json")
    assert loaded == record
    assert latest == record


def test_artifact_log_records_writes_and_latest_copies(tmp_path: Path):
    write_text_artifact(
        tmp_path / "prompt-20260423.md", tmp_path / "prompt.md", "# Task\n"
    )

    log = (tmp_path / "artifacts.log").read_text(encoding="utf-8")

    assert " - wrote prompt-20260423.md\n" in log
    assert " - updated prompt.md from prompt-20260423.md\n" in log


def test_codex_token_usage_records_run_and_total(tmp_path: Path):
    first = tmp_path / "codex-20260423.log"
    first.write_text(
        "Token usage: input tokens: 100\noutput tokens: 20\ntotal tokens: 120\n",
        encoding="utf-8",
    )
    record_artifact_file(first)
    record_codex_token_usage(tmp_path, first)
    second = tmp_path / "codex-20260423-review-1.log"
    second.write_text(
        '{"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n',
        encoding="utf-8",
    )
    record_artifact_file(second)
    record_codex_token_usage(tmp_path, second)

    log = (tmp_path / "artifacts.log").read_text(encoding="utf-8")

    assert "tokens codex-20260423.log: input=100 output=20 total=120" in log
    assert "tokens codex-20260423-review-1.log: input=10 output=5 total=15" in log
    assert "tokens total: input=110 output=25 total=135 across 2/2 codex run(s)" in log


def test_codex_token_usage_parses_codex_tokens_used_summary(tmp_path: Path):
    log_path = tmp_path / "codex-20260426-005740.log"
    log_path.write_text("some output\n\ntokens used\n23,690\n", encoding="utf-8")
    record_artifact_file(log_path)
    record_codex_token_usage(tmp_path, log_path)

    log = (tmp_path / "artifacts.log").read_text(encoding="utf-8")

    assert "tokens codex-20260426-005740.log: total=23690" in log
    assert "tokens total: total=23690 across 1/1 codex run(s)" in log
