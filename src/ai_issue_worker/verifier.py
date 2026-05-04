from __future__ import annotations

from pathlib import Path

from .config import VerifyConfig
from .models import CommandResult, VerifyResult
from .shell import run_cmd


def run_verifier(config: VerifyConfig, worktree_path: Path, log_path: Path | None = None) -> VerifyResult:
    results: list[CommandResult] = []
    for command in config.commands:
        result = run_cmd(command, cwd=worktree_path)
        results.append(result)
        if result.exit_code != 0 and not config.run_all_commands:
            break
    verify = VerifyResult(passed=all(result.exit_code == 0 for result in results), commands=results)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(format_verification_summary(verify, include_full=True), encoding="utf-8")
    return verify


def _tail(text: str, limit: int = 1200) -> str:
    return text[-limit:] if len(text) > limit else text


def format_verification_summary(result: VerifyResult, include_full: bool = False) -> str:
    parts: list[str] = []
    for command in result.commands:
        status = "PASS" if command.exit_code == 0 else "FAIL"
        stdout = command.stdout if include_full else _tail(command.stdout)
        stderr = command.stderr if include_full else _tail(command.stderr)
        parts.append(
            f"{status} {command.command}\n"
            f"exit code: {command.exit_code}\n"
            f"duration: {command.duration_sec:.2f}s\n"
            f"stdout:\n{stdout}\n"
            f"stderr:\n{stderr}\n"
        )
    return "\n".join(parts).strip() or "No verifier commands were run."

