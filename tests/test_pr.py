from ai_issue_worker.config import PRConfig
from ai_issue_worker.models import CommandResult, DiffSummary, Issue, VerifyResult
from ai_issue_worker.pr import build_pr_body
from ai_issue_worker.verifier import format_verification_summary


def test_pr_body_masks_user_home_paths():
    verify = VerifyResult(
        True,
        [
            CommandResult(
                "pytest",
                0,
                "opened /Users/alice/Repos/project/tests/test_app.py",
                "",
                0.1,
            )
        ],
    )

    body = build_pr_body(
        PRConfig(),
        Issue(1, "Fix bug", "", [], "open"),
        format_verification_summary(verify),
        DiffSummary(["src/app.py"], "", 1, False, None),
    )

    assert "/Users/alice" not in body
    assert "####/Repos/project/tests/test_app.py" in body
