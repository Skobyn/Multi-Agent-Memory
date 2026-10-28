"""`em-hook doctor` makes one call per component and reports pass/fail without ever raising."""
from fakes import FakeDM, FakeLLM

from epistemic_monitor import doctor


def test_doctor_reports_each_component(cfg, monkeypatch, capsys):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-1234567890abc")
    monkeypatch.setattr("epistemic_monitor.decision_model.make_decision_model", lambda c, fb=None: FakeDM(default=0.9))
    monkeypatch.setattr("epistemic_monitor.llm.make_llm", lambda sec: FakeLLM(["OK"]))
    monkeypatch.setenv("EPISTEMIC_EMBEDDINGS_PROVIDER", "none")
    from epistemic_monitor.config import load_config
    checks = doctor.run_checks(load_config())
    by = {c["name"].split(" (")[0]: c for c in checks}
    assert by["python"]["ok"] and "TYPESAFE_API_KEY=ts-1…abc" in by["keys in environment"]["detail"]
    assert next(c for c in checks if c["name"].startswith("decision model"))["ok"] is True
    assert next(c for c in checks if c["name"].startswith("advisor"))["ok"] is True
    assert next(c for c in checks if c["name"].startswith("embeddings"))["ok"] is None
    assert doctor.main([]) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "PASS decision model" in out


def test_doctor_fails_on_broken_required_component(cfg, monkeypatch, capsys):
    def boom(c, fb=None):
        raise RuntimeError("HTTP 401 from typesafe")
    monkeypatch.setattr("epistemic_monitor.decision_model.make_decision_model", boom)
    monkeypatch.setattr("epistemic_monitor.llm.make_llm", lambda sec: FakeLLM(["OK"]))
    monkeypatch.setenv("EPISTEMIC_EMBEDDINGS_PROVIDER", "none")
    assert doctor.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL decision model" in out and "HTTP 401" in out and "RESULT: FAIL" in out
