from ai_issue_worker.worktree import branch_name


def test_branch_slug_shape():
    result = branch_name("ai/issue-", 123, 'Fix parser error in Foo/Bar!')
    assert result == "ai/issue-123-fix-parser-error-in-foo-bar"


def test_branch_slug_respects_max_length():
    result = branch_name("ai/issue-", 123, "x" * 200, max_length=80)
    assert len(result) <= 80

