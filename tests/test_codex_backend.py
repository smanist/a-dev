from ai_issue_worker.codex_backend import codex_command_args


def test_bare_codex_command_uses_noninteractive_exec():
    assert codex_command_args("codex") == ["codex", "exec", "--full-auto", "-"]


def test_codex_exec_command_reads_prompt_from_stdin():
    assert codex_command_args("codex exec --full-auto") == ["codex", "exec", "--full-auto", "-"]


def test_codex_exec_command_adds_model_and_reasoning():
    assert codex_command_args("codex exec --full-auto", model="gpt-5.4", reasoning="xhigh") == [
        "codex",
        "exec",
        "--full-auto",
        "--model",
        "gpt-5.4",
        "-c",
        'model_reasoning_effort="xhigh"',
        "-",
    ]


def test_codex_exec_command_keeps_explicit_model_and_reasoning():
    assert codex_command_args(
        'codex exec --full-auto --model gpt-5.5 -c model_reasoning_effort="high"',
        model="gpt-5.4",
        reasoning="xhigh",
    ) == [
        "codex",
        "exec",
        "--full-auto",
        "--model",
        "gpt-5.5",
        "-c",
        "model_reasoning_effort=high",
        "-",
    ]


def test_custom_command_is_preserved():
    assert codex_command_args("my-agent --flag") == ["my-agent", "--flag"]
