"""Decision-model adapter (DESIGN.md §8.2).

``TypeSafeDecisionModel`` calls ``POST /v1/systemone`` (verified live on
2026-10-27). ``LLMDecisionModel`` is the configurable fallback: a chat model
asked for a verbalized probability. Both log the same fields so calibration
is per (provider, model).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class NoulResult:
    p_true: float
    latency_ms: int
    model: str
    input_tokens: int = 0

    @property
    def confidence(self) -> float:
        """Noul has no confidence field; TypeSafe's docs define it as |2p-1|."""
        return abs(2 * self.p_true - 1)


@dataclass
class ScoreResult:
    expected: float
    probabilities: dict[str, float]
    confidence: float
    latency_ms: int
    model: str


class DecisionModel(Protocol):
    name: str

    def noul(self, instructions: str, state: Any, criteria: dict[str, str] | None = None) -> NoulResult: ...

    def score(self, instructions: str, state: Any, levels: list[str]) -> ScoreResult: ...


class _TokenBucket:
    def __init__(self, rate_per_s: float, burst: int | None = None) -> None:
        self.rate, self.capacity = float(rate_per_s), float(burst or max(1, int(rate_per_s)))
        self.tokens, self.updated, self.lock = self.capacity, time.monotonic(), threading.Lock()

    def acquire(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)


class DecisionModelError(RuntimeError):
    pass


class TypeSafeDecisionModel:
    def __init__(self, model: str = "jev-latest", base_url: str = "https://api.typesafe.ai",
                 timeout_ms: int = 1500, max_rps: float = 20, api_key: str | None = None,
                 max_retries: int = 2) -> None:
        self.model, self.base_url, self.timeout = model, base_url.rstrip("/"), timeout_ms / 1000
        self.api_key = api_key if api_key is not None else os.environ.get("TYPESAFE_API_KEY", "")
        self.bucket, self.max_retries = _TokenBucket(max_rps), max_retries
        self.name = f"typesafe:{model}"

    def ask(self, state: Any, questions: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], int]:
        """Raw call: returns (response_json, latency_ms). Retries 429/529 with backoff."""
        body = json.dumps({"model": self.model, "state": state, "questions": questions}).encode()
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        attempt, delay = 0, 0.25
        while True:
            if not self.bucket.acquire():
                raise DecisionModelError("client rate limiter timed out")
            t0 = time.monotonic()
            req = urllib.request.Request(f"{self.base_url}/v1/systemone", data=body, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode())
                return data, int((time.monotonic() - t0) * 1000)
            except urllib.error.HTTPError as e:
                if e.code in (429, 529) and attempt < self.max_retries:
                    ra = e.headers.get("retry-after") if e.headers else None
                    time.sleep(float(ra) if ra and ra.replace(".", "", 1).isdigit() else delay)
                    attempt, delay = attempt + 1, delay * 2
                    continue
                detail = e.read().decode(errors="replace")[:300] if hasattr(e, "read") else ""
                raise DecisionModelError(f"HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise DecisionModelError(str(e)) from e

    def noul(self, instructions: str, state: Any, criteria: dict[str, str] | None = None) -> NoulResult:
        q: dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            q["criteria"] = {"true": criteria.get("true", ""), "false": criteria.get("false", "")}
        data, ms = self.ask(state, {"q": q})
        return NoulResult(float(data["answers"]["q"]["noul"]), ms, str(data.get("model", self.model)),
                          int(data.get("usage", {}).get("input_tokens", 0)))

    def score(self, instructions: str, state: Any, levels: list[str]) -> ScoreResult:
        data, ms = self.ask(state, {"q": {"type": "score", "instructions": instructions, "criteria": levels}})
        a = data["answers"]["q"]
        return ScoreResult(float(a["score"]), {str(k): float(v) for k, v in a.get("probabilities", {}).items()},
                           float(a.get("confidence", 0.0)), ms, str(data.get("model", self.model)))

    def noul_and_score(self, noul_instructions: str, criteria: dict[str, str], score_instructions: str,
                       levels: list[str], state: Any) -> tuple[NoulResult, ScoreResult]:
        """Both gate questions in one request (TypeSafe evaluates them in parallel against one state)."""
        data, ms = self.ask(state, {
            "n": {"type": "noul", "instructions": noul_instructions,
                  "criteria": {"true": criteria.get("true", ""), "false": criteria.get("false", "")}},
            "s": {"type": "score", "instructions": score_instructions, "criteria": levels}})
        n, sc = data["answers"]["n"], data["answers"]["s"]
        model = str(data.get("model", self.model))
        return (NoulResult(float(n["noul"]), ms, model, int(data.get("usage", {}).get("input_tokens", 0))),
                ScoreResult(float(sc["score"]), {str(k): float(v) for k, v in sc.get("probabilities", {}).items()},
                            float(sc.get("confidence", 0.0)), ms, model))


class LLMDecisionModel:
    """Fallback: an OpenAI-compatible chat endpoint (OpenRouter) or the Anthropic
    Messages API, prompted for a JSON probability."""

    def __init__(self, provider: str = "openrouter", model: str = "anthropic/claude-haiku-5-5",
                 timeout_ms: int = 8000, api_key: str | None = None) -> None:
        self.provider, self.model, self.timeout = provider, model, timeout_ms / 1000
        env = "OPENROUTER_API_KEY" if provider == "openrouter" else "ANTHROPIC_API_KEY"
        self.api_key = api_key if api_key is not None else os.environ.get(env, "")
        self.name = f"llm:{provider}:{model}"

    def _complete(self, prompt: str) -> tuple[str, int]:
        t0 = time.monotonic()
        headers = {"content-type": "application/json"}
        if self.provider == "openrouter":
            url = "https://openrouter.ai/api/v1/chat/completions"
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            body = {"model": self.model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 64, "temperature": 0}
            extract = lambda d: d["choices"][0]["message"]["content"]
        else:
            url = "https://api.anthropic.com/v1/messages"
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            body = {"model": self.model, "max_tokens": 64, "messages": [{"role": "user", "content": prompt}]}
            extract = lambda d: "".join(b.get("text", "") for b in d.get("content", []))
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise DecisionModelError(str(e)) from e
        return extract(data), int((time.monotonic() - t0) * 1000)

    @staticmethod
    def _parse_p(text: str) -> float:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return max(0.0, min(1.0, float(json.loads(text[start:end + 1]).get("p", 0.5))))
            except (ValueError, TypeError):
                pass
        return 0.5

    def noul(self, instructions: str, state: Any, criteria: dict[str, str] | None = None) -> NoulResult:
        crit = ""
        if criteria:
            crit = f"\nAnswer YES when: {criteria.get('true', '')}\nAnswer NO when: {criteria.get('false', '')}"
        prompt = (f"Question: {instructions}{crit}\n\nState:\n{json.dumps(state, ensure_ascii=False)[:60000]}\n\n"
                  'Reply with only JSON: {"p": <probability between 0 and 1 that the answer is YES>}')
        text, ms = self._complete(prompt)
        return NoulResult(self._parse_p(text), ms, self.model)

    def score(self, instructions: str, state: Any, levels: list[str]) -> ScoreResult:
        lv = "\n".join(f"{i}: {l}" for i, l in enumerate(levels))
        prompt = (f"Rate the state. {instructions}\nLevels:\n{lv}\n\nState:\n{json.dumps(state, ensure_ascii=False)[:60000]}\n\n"
                  'Reply with only JSON: {"p": <expected level as a number>}')
        text, ms = self._complete(prompt)
        start, end = text.find("{"), text.rfind("}")
        try:
            expected = float(json.loads(text[start:end + 1]).get("p", 0.0))
        except (ValueError, TypeError):
            expected = 0.0
        return ScoreResult(expected, {}, 0.0, ms, self.model)


def make_decision_model(cfg: dict, fallback_cfg: dict | None = None) -> DecisionModel:
    provider = cfg.get("provider", "typesafe")
    if provider == "typesafe":
        return TypeSafeDecisionModel(cfg.get("model", "jev-latest"), cfg.get("base_url", "https://api.typesafe.ai"),
                                     int(cfg.get("timeout_ms", 1500)), float(cfg.get("max_rps", 20)))
    if provider == "llm":
        fb = fallback_cfg or {}
        return LLMDecisionModel(fb.get("provider", "openrouter"), fb.get("model", "anthropic/claude-haiku-5-5"),
                                int(fb.get("timeout_ms", 8000)))
    raise ValueError(f"unsupported decision_model provider {provider!r} (typesafe | llm)")
