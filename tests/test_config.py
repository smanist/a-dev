from pathlib import Path

import pytest  # type: ignore[reportMissingImports]

from ai_issue_worker.config import ConfigError, config_from_dict, load_config, write_default_config


def test_default_config_generation_works(tmp_path: Path):
    path = tmp_path / ".ai-issue-worker.yaml"
    write_default_config(path)
    config = load_config(path)
    assert config.repo == "owner/repo"
    assert config.issue_selection.ready_label == "ai-ready"
    assert config.issue_selection.resume_label == "ai-resume"
    assert config.issue_selection.respect_issue_dependencies is True
    assert config.issue_selection.allow_stacked_prs is False
    assert config.issue_selection.max_stack_depth == 3
    assert config.agent.model == "gpt-5.4"
    assert config.agent.reasoning == "high"
    assert config.review.enabled is True
    assert config.review.command == "codex exec --sandbox read-only"
    assert config.review.max_iterations == 3
    assert config.review.fix_priorities == ["P0", "P1"]


def test_default_config_can_use_inferred_repo_and_branch(tmp_path: Path):
    path = tmp_path / ".ai-issue-worker.yaml"
    write_default_config(path, repo="owner/repo", base_branch="trunk")
    config = load_config(path)
    assert config.repo == "owner/repo"
    assert config.base_branch == "trunk"


def test_config_missing_repo_fails_clearly():
    with pytest.raises(ConfigError, match="repo"):
        config_from_dict({"base_branch": "main"})


def test_config_accepts_agent_model_and_reasoning():
    config = config_from_dict({"repo": "owner/repo", "agent": {"model": "gpt-5.4", "reasoning": "xhigh"}})
    assert config.agent.model == "gpt-5.4"
    assert config.agent.reasoning == "xhigh"


def test_config_accepts_legacy_reasoning_effort():
    config = config_from_dict({"repo": "owner/repo", "agent": {"reasoning_effort": "xhigh"}})
    assert config.agent.reasoning == "xhigh"


def test_config_rejects_unknown_reasoning():
    with pytest.raises(ConfigError, match="reasoning"):
        config_from_dict({"repo": "owner/repo", "agent": {"reasoning": "max"}})


def test_config_accepts_review_overrides():
    config = config_from_dict(
        {"repo": "owner/repo", "review": {"enabled": False, "max_iterations": 2, "fix_priorities": ["P1"]}}
    )
    assert config.review.enabled is False
    assert config.review.max_iterations == 2
    assert config.review.fix_priorities == ["P1"]


def test_config_accepts_disabling_issue_dependency_selection():
    config = config_from_dict({"repo": "owner/repo", "issue_selection": {"respect_issue_dependencies": False}})

    assert config.issue_selection.respect_issue_dependencies is False


def test_config_accepts_stacked_pr_selection():
    config = config_from_dict(
        {
            "repo": "owner/repo",
            "issue_selection": {
                "allow_stacked_prs": True,
                "max_stack_depth": 2,
            },
        }
    )

    assert config.issue_selection.allow_stacked_prs is True
    assert config.issue_selection.max_stack_depth == 2


def test_config_rejects_invalid_stack_depth():
    with pytest.raises(ConfigError, match="issue_selection.max_stack_depth"):
        config_from_dict({"repo": "owner/repo", "issue_selection": {"max_stack_depth": 0}})


def test_config_rejects_invalid_review_max_iterations():
    with pytest.raises(ConfigError, match="review.max_iterations"):
        config_from_dict({"repo": "owner/repo", "review": {"max_iterations": 0}})


def test_config_rejects_invalid_review_priority():
    with pytest.raises(ConfigError, match="review.fix_priorities"):
        config_from_dict({"repo": "owner/repo", "review": {"fix_priorities": ["P2"]}})


def test_init_refuses_overwrite_without_force(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("repo: owner/repo\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="already exists"):
        write_default_config(path)
