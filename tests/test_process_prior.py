"""A process falsifier is satisfied only by a command that RUNS its prior step (SkillCFI arXiv 2611.24185:
obligations are enforceable only on evidence that crosses the tool boundary; review 2026-11-28). Before, the
prior_pattern was searched in the raw command text, so `echo pytest` cleared the next commit with no model call."""
import pytest
from fakes import FakeDM
from helpers import make_entry

from epistemic_monitor.bash_classifier import runs_matching
from epistemic_monitor.gate import GatePipeline

FALS = [{"kind": "command_requires_prior", "pattern": r"\bgit\s+commit\b", "prior_pattern": r"\bpytest\b", "window": 30}]


@pytest.mark.parametrize("prior", ["echo pytest", "grep -r pytest .", "git log --grep pytest", "pytest --co -q",
                                   "pytest --collect-only", "cat pytest.ini", "rg 'pytest' src | head",
                                   "ls tests  # then pytest", "printf 'pytest\\n'"])
def test_inert_mention_does_not_satisfy(cfg, store, prior):
    e = make_entry(store, type="process", statement="Run pytest before every commit.", falsifiers=FALS)
    dm = FakeDM(default=0.1)
    res = GatePipeline(store, cfg, dm=dm, llm=None).evaluate(
        "Bash", {"command": "git commit -m x"}, "s1", history=[{"tool": "Bash", "command": prior, "decision": "allow"}])
    assert [t.kind for t in res.trips] == ["command_requires_prior"] and e.id not in res.cleared


@pytest.mark.parametrize("prior", ["pytest -q", "python -m pytest tests", "cd app && pytest -x", "pytest -q 2>&1 | tail -20",
                                   "FOO=1 pytest", "uv run pytest", "bash -c 'pytest -q'", "timeout 600 pytest",
                                   "echo start; pytest", "docker compose run app pytest", "/usr/bin/env pytest -q",
                                   "pytest || true", "(pytest -q)", "echo $(pytest -q)"])
def test_real_run_satisfies(cfg, store, prior):
    e = make_entry(store, type="process", statement="Run pytest before every commit.", falsifiers=FALS)
    res = GatePipeline(store, cfg, dm=FakeDM(default=0.97), llm=None).evaluate(
        "Bash", {"command": "git commit -m x"}, "s1", history=[{"tool": "Bash", "command": prior, "decision": "allow"}])
    assert res.trips == [] and res.cleared == [e.id] and res.decision == "allow"


@pytest.mark.parametrize("cmd", ['grep -rn "git commit" docs', "echo 'remember: git commit after tests'",
                                 "git log --grep 'git commit'"])
def test_inert_mention_of_trigger_does_not_trip(cfg, store, cmd):
    make_entry(store, type="process", statement="Run pytest before every commit.", falsifiers=FALS)
    res = GatePipeline(store, cfg, dm=FakeDM(default=0.1), llm=None).evaluate("Bash", {"command": cmd}, "s1", history=[])
    assert res.trips == [] and res.decision != "deny"


@pytest.mark.parametrize("cmd", ["git add -A && git commit -m x", "cd repo; git commit -am wip", "sudo git commit -m x",
                                 "echo done && git commit -m x"])
def test_real_trigger_still_trips(cfg, store, cmd):
    make_entry(store, type="process", statement="Run pytest before every commit.", falsifiers=FALS)
    res = GatePipeline(store, cfg, dm=None, llm=None).evaluate("Bash", {"command": cmd}, "s1", history=[])
    assert [t.kind for t in res.trips] == ["command_requires_prior"]


def test_pattern_spanning_an_operator_falls_back_to_whole_text():
    assert runs_matching("make build && make test", r"build\s*&&\s*make test")
    assert not runs_matching("echo hi", r"pytest")


def test_bad_pattern_raises():
    with pytest.raises(Exception):
        runs_matching("pytest", "(")
