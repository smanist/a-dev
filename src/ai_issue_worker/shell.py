from __future__ import annotations

import shlex
import subprocess
import time
from pathlib import Path

from .models import CommandResult


def _timeout_output(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


def command_to_text(args: list[str] | str) -> str:
    if isinstance(args, str):
        return args
    return " ".join(shlex.quote(part) for part in args)


def run_cmd(
    args: list[str] | str,
    cwd: Path | None = None,
    timeout: int | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> CommandResult:
    started = time.monotonic()
    shell = isinstance(args, str)
    try:
        completed = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            timeout=timeout,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            shell=shell,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = _timeout_output(exc.stdout)
        stderr = _timeout_output(exc.stderr)
        stderr += f"\nCommand timed out after {timeout} seconds."
    except FileNotFoundError as exc:
        exit_code = 127
        stdout = ""
        stderr = str(exc)
    duration = time.monotonic() - started
    return CommandResult(command=command_to_text(args), exit_code=exit_code, stdout=stdout, stderr=stderr, duration_sec=duration)
