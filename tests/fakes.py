"""Test doubles for the LLM and decision-model roles."""

from __future__ import annotations

import json
from typing import Any

from epistemic_monitor.decision_model import NoulResult, ScoreResult
from epistemic_monitor.llm import Completion


class FakeLLM:
    """Returns queued JSON objects (dicts) or raw strings, in order; repeats the last one."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []
        self.name = "fake"
        self.model = "fake"

    def _next(self) -> Any:
        if not self.responses:
            return {"entries": [], "supersedes": []}
        r = self.responses[0]
        if len(self.responses) > 1:
            self.responses.pop(0)
        return r

    def complete(self, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0) -> Completion:
        self.calls.append((system, user))
        r = self._next()
        text = r if isinstance(r, str) else json.dumps(r)
        return Completion(text=text, latency_ms=1, model="fake", input_tokens=len(user) // 4, output_tokens=len(text) // 4)

    def complete_json(self, system: str, user: str, max_tokens: int = 4096):
        comp = self.complete(system, user, max_tokens)
        from epistemic_monitor.llm import LLMError, extract_json
        parsed = extract_json(comp.text)
        if parsed is None:
            raise LLMError("model did not return JSON: " + comp.text[:200])
        return parsed, comp


class FakeDM:
    """noul() answers from a map keyed by decision id (entailment) or a constant."""

    def __init__(self, by_id: dict[str, float] | None = None, default: float = 0.9, fail_ids: set[str] | None = None) -> None:
        self.by_id, self.default, self.fail_ids = by_id or {}, default, fail_ids or set()
        self.calls: list[dict] = []
        self.name = "fake-dm"

    def noul(self, instructions: str, state: Any, criteria: dict[str, str] | None = None) -> NoulResult:
        self.calls.append({"instructions": instructions, "state": state})
        did = None
        if isinstance(state, dict) and isinstance(state.get("decision"), dict):
            did = state["decision"].get("id")
        if did in self.fail_ids:
            from epistemic_monitor.decision_model import DecisionModelError
            raise DecisionModelError("simulated outage")
        return NoulResult(self.by_id.get(did, self.default), 1, "fake-dm")

    def score(self, instructions: str, state: Any, levels: list[str]) -> ScoreResult:
        return ScoreResult(0.0, {}, 1.0, 1, "fake-dm")
