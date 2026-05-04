from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import AgentResult


class AgentBackend(Protocol):
    def run(self, worktree_path: Path, prompt_path: Path, timeout_sec: int) -> AgentResult:
        ...

