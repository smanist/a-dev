from pathlib import Path

from ai_issue_worker.config import VerifyConfig
from ai_issue_worker.verifier import run_verifier


def test_verifier_passes_all_commands(tmp_path: Path):
    result = run_verifier(VerifyConfig(commands=["python -c \"print('ok')\""]), tmp_path)
    assert result.passed
    assert result.commands[0].exit_code == 0


def test_verifier_fails_and_runs_all_when_configured(tmp_path: Path):
    result = run_verifier(
        VerifyConfig(
            commands=['python -c "import sys; sys.exit(1)"', "python -c \"print('still runs')\""],
            run_all_commands=True,
        ),
        tmp_path,
    )
    assert not result.passed
    assert len(result.commands) == 2


def test_verifier_stops_on_first_failure(tmp_path: Path):
    result = run_verifier(
        VerifyConfig(
            commands=['python -c "import sys; sys.exit(1)"', "python -c \"print('skip')\""],
            run_all_commands=False,
        ),
        tmp_path,
    )
    assert not result.passed
    assert len(result.commands) == 1
