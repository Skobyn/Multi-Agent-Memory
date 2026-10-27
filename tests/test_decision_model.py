import io
import json
import os
import urllib.error

import pytest

from epistemic_monitor import decision_model as dm


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _canned(answers):
    return _Resp(json.dumps({"model": "jev-1.14.0", "answers": answers,
                             "usage": {"input_tokens": 120, "output_tokens": 12}}).encode())


def test_typesafe_noul_and_score(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body)
        assert req.full_url.endswith("/v1/systemone") and body["model"] == "jev-latest"
        q = body["questions"]["q"]
        if q["type"] == "noul":
            return _canned({"q": {"type": "noul", "noul": 0.93}})
        return _canned({"q": {"type": "score", "score": 2.4, "confidence": 0.8,
                              "legend": {"0": "a", "1": "b", "2": "c"}, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7}}})

    monkeypatch.setattr(dm.urllib.request, "urlopen", fake_urlopen)
    m = dm.TypeSafeDecisionModel(api_key="k", max_rps=100)
    r = m.noul("Does the change conflict?", {"decision": "pg", "change": "mongo"}, {"true": "conflict", "false": "fine"})
    assert r.p_true == 0.93 and r.confidence == pytest.approx(0.86) and r.input_tokens == 120
    assert calls[0]["questions"]["q"]["criteria"] == {"true": "conflict", "false": "fine"}
    s = m.score("How serious?", {"x": 1}, ["harmless", "minor", "serious"])
    assert s.expected == 2.4 and s.probabilities["2"] == 0.7 and s.confidence == 0.8


def test_typesafe_retries_on_429_then_fails(monkeypatch):
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"retry-after": "0"}, io.BytesIO(b"{}"))

    monkeypatch.setattr(dm.urllib.request, "urlopen", fake_urlopen)
    m = dm.TypeSafeDecisionModel(api_key="k", max_rps=100, max_retries=2)
    with pytest.raises(dm.DecisionModelError):
        m.noul("q", "s")
    assert attempts["n"] == 3


def test_llm_probability_parsing():
    assert dm.LLMDecisionModel._parse_p('Sure: {"p": 0.8}') == 0.8
    assert dm.LLMDecisionModel._parse_p("garbage") == 0.5
    assert dm.LLMDecisionModel._parse_p('{"p": 7}') == 1.0


def test_factory():
    assert dm.make_decision_model({"provider": "typesafe"}).name.startswith("typesafe:")
    assert dm.make_decision_model({"provider": "llm"}, {"provider": "openrouter", "model": "m"}).name == "llm:openrouter:m"
    with pytest.raises(ValueError):
        dm.make_decision_model({"provider": "nope"})


@pytest.mark.skipif(os.environ.get("EM_LIVE_TESTS") != "1", reason="set EM_LIVE_TESTS=1 to call the live TypeSafe API")
def test_live_gate_question_separates_reversal_from_consistent_change():
    m = dm.TypeSafeDecisionModel()
    decision = {"id": "D-0042", "statement": "Use PostgreSQL as the primary database.", "provenance": "user"}
    crit = {"true": "The change introduces, switches to, or depends on something the decision rules out.",
            "false": "The change is consistent with the decision or unrelated to it."}
    rev = m.noul("Does the planned change conflict with the decision?",
                 {"decision": decision, "planned_change": {"diff": "+from pymongo import MongoClient\n+db = MongoClient(URL)"}}, crit)
    ok = m.noul("Does the planned change conflict with the decision?",
                {"decision": decision, "planned_change": {"diff": "+engine = create_engine(URL, pool_size=10)"}}, crit)
    assert rev.p_true > 0.8 > 0.3 > ok.p_true
    assert rev.latency_ms < 2000
