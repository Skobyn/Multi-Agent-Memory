import os
from epistemic_monitor.llm import extract_json, resolve_provider, to_openrouter_model


def test_extract_json_handles_fences_prose_and_nesting():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"entries": [{"x": "a } b"}], "n": {"m": 2}} trailing') == {"entries": [{"x": "a } b"}], "n": {"m": 2}}
    assert extract_json("no json here") is None
    assert extract_json("[1,2]") is None
    assert extract_json('{"broken": ') is None


def test_model_mapping_and_provider_resolution():
    assert to_openrouter_model("claude-haiku-5-5") == "anthropic/claude-haiku-5.5"
    assert to_openrouter_model("claude-opus-5-5") == "anthropic/claude-opus-5.5"
    assert to_openrouter_model("anthropic/claude-sonnet-5.5") == "anthropic/claude-sonnet-5.5"
    assert resolve_provider("auto", {"ANTHROPIC_API_KEY": "k"}) == "anthropic"
    assert resolve_provider("auto", {}) == "openrouter"
    assert resolve_provider("openrouter", {"ANTHROPIC_API_KEY": "k"}) == "openrouter"


def _fake_claude(tmp_path, monkeypatch, body: str, exit_code: int = 0):
    """A stand-in `claude` on PATH: records its argv and stdin, prints `body`."""
    log = tmp_path / "calls.json"
    script = tmp_path / "claude"
    script.write_text("#!/usr/bin/env python3\nimport json, os, sys\n"
                      f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'stdin': sys.stdin.read(), 'env': dict(os.environ)}}) + chr(10))\n"
                      f"sys.stdout.write({body!r})\nsys.exit({exit_code})\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")
    return log


def test_claude_cli_provider_runs_one_bare_turn(tmp_path, monkeypatch):
    import json as _json
    from epistemic_monitor.llm import LLMClient, make_llm
    body = _json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{\"verdict\": \"consistent\"}",
                        "usage": {"input_tokens": 12, "cache_read_input_tokens": 300, "output_tokens": 7}, "total_cost_usd": 0.01})
    log = _fake_claude(tmp_path, monkeypatch, body)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "parent")
    monkeypatch.setenv("CLAUDE_CODE_PLUGIN_DIRS", "/plugins/a:/plugins/b")
    llm = make_llm({"provider": "claude-cli", "model": "claude-opus-5-5", "timeout_ms": 5000})
    assert llm.provider == "claude-cli" and llm.name == "claude-cli:claude-opus-5-5"
    parsed, comp = llm.complete_json("You are the advisor.", "Is this consistent?")
    assert parsed == {"verdict": "consistent"} and comp.input_tokens == 312 and comp.output_tokens == 7
    call = _json.loads(log.read_text().splitlines()[0])
    argv = call["argv"]
    assert argv[0] == "-p" and "--no-session-persistence" in argv and "--tools" in argv and "--bare" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "project"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5" and argv[argv.index("--system-prompt") + 1] == "You are the advisor."
    assert argv[argv.index("--max-turns") + 1] == "1" and call["stdin"] == "Is this consistent?"
    # the nested session must never load the plugin or be taken for the parent session
    assert "CLAUDE_CODE_PLUGIN_DIRS" not in call["env"] and "CLAUDE_SESSION_ID" not in call["env"]
    assert call["env"]["EPISTEMIC_MONITOR_CHILD"] == "1"
    assert LLMClient("cli", "claude-haiku-5-5").provider == "claude-cli"


def test_claude_cli_provider_reports_failures(tmp_path, monkeypatch):
    import json as _json
    import pytest as _pytest
    from epistemic_monitor.llm import LLMClient, LLMError
    _fake_claude(tmp_path, monkeypatch, _json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": "boom"}))
    with _pytest.raises(LLMError, match="error_max_turns"):
        LLMClient("claude-cli", "claude-opus-5-5", max_retries=0).complete("s", "u")
    _fake_claude(tmp_path, monkeypatch, "not json", exit_code=1)
    with _pytest.raises(LLMError, match="exit 1"):
        LLMClient("claude-cli", "claude-opus-5-5", max_retries=0).complete("s", "u")
    with _pytest.raises(LLMError, match="cannot run"):
        LLMClient("claude-cli", "claude-opus-5-5", max_retries=0, command="/nonexistent/claude").complete("s", "u")
