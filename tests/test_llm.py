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
