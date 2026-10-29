"""Minimal chat-completion client for the extractor and advisor roles.

Providers: ``anthropic`` (Messages API), ``openrouter`` (OpenAI-compatible),
``claude-cli`` (one tool-less turn of ``claude -p``, so the call runs on whatever the
Claude Code CLI is signed in with, a subscription included; the sanctioned way to use a
subscription for the plugin's own model calls), ``auto`` (Anthropic when
``ANTHROPIC_API_KEY`` is set, else OpenRouter).
Model ids are written Anthropic-style in config (``claude-haiku-5-5``) and
mapped to OpenRouter's ``anthropic/claude-haiku-5.5`` form when needed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    latency_ms: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw: dict = field(default_factory=dict)


def to_openrouter_model(model: str) -> str:
    if "/" in model:
        return model
    return "anthropic/" + re.sub(r"-(\d)-(\d)$", r"-\1.\2", model)


CLI_ALIASES = {"claude-cli", "claude_cli", "cli"}
_CHILDREN: set = set()


def _kill_group(proc) -> None:  # noqa: ANN001
    try:
        os.killpg(proc.pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def kill_children() -> None:
    """Kill every `claude -p` this process started (used when the hook itself is being terminated)."""
    for proc in list(_CHILDREN):
        _kill_group(proc)
    _CHILDREN.clear()


def resolve_provider(provider: str, environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if provider == "auto":
        return "anthropic" if env.get("ANTHROPIC_API_KEY") else "openrouter"
    if provider in CLI_ALIASES:
        return "claude-cli"
    return provider


class LLMClient:
    def __init__(self, provider: str, model: str, timeout_ms: int = 30000, api_key: str | None = None,
                 base_url: str | None = None, max_retries: int = 1, command: str | None = None) -> None:
        self.provider = resolve_provider(provider)
        self.model = to_openrouter_model(model) if self.provider == "openrouter" else model
        self.timeout = timeout_ms / 1000
        self.max_retries = max_retries
        self.command = command or os.environ.get("EPISTEMIC_CLAUDE_BIN") or "claude"
        if self.provider == "claude-cli":
            self.api_key, self.base_url = "", ""
        elif self.provider == "anthropic":
            self.api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
            self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        elif self.provider == "openrouter":
            self.api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
            self.base_url = (base_url or "https://openrouter.ai/api/v1").rstrip("/")
        else:
            raise ValueError(f"unsupported LLM provider {provider!r} (anthropic | openrouter | claude-cli | auto)")
        self.name = f"{self.provider}:{self.model}"

    def _request_cli(self, system: str, user: str) -> tuple[str, dict]:
        """One tool-less, hook-less, unpersisted turn of `claude -p`. Runs in a scratch directory so the nested
        session writes nothing into the project and loads none of its settings; the parent session's id is
        dropped from the environment so the call is not recorded as part of it."""
        scratch = os.path.join(tempfile.gettempdir(), "epistemic-monitor-cli")
        os.makedirs(scratch, exist_ok=True)
        from .throttle import child_env
        env = child_env()   # no CLAUDE_CODE_PLUGIN_DIRS, no parent session, EPISTEMIC_MONITOR_CHILD=1: the nested
        #                     session loads no plugin and, if it somehow did, every hook would exit at once
        # `--setting-sources project` with a scratch cwd loads no settings and no hooks (`--bare` would, but it also
        # skips the CLI's stored credentials in some installs, which is the whole point of this provider).
        args = [self.command, "-p", "--no-session-persistence", "--setting-sources", "project", "--output-format", "json",
                "--max-turns", "1", "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--model", self.model, "--system-prompt", system]
        try:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    cwd=scratch, env=env, start_new_session=True)
        except OSError as e:
            raise LLMError(f"claude-cli: cannot run {self.command!r}: {e}") from e
        _CHILDREN.add(proc)
        try:
            out, err = proc.communicate(user, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            _kill_group(proc)
            raise LLMError(f"claude-cli: timed out after {self.timeout:.0f}s") from e
        finally:
            _CHILDREN.discard(proc)
        p = subprocess.CompletedProcess(args, proc.returncode, out, err)
        lines = [l for l in p.stdout.splitlines() if l.startswith("{")]
        data: dict[str, Any] = {}
        if lines:
            try:
                data = json.loads(lines[-1])
            except ValueError:
                data = {}
        if p.returncode != 0 or data.get("is_error") or (data and data.get("subtype") not in (None, "success")):
            detail = (str(data.get("result") or "") or p.stderr)[-400:]
            raise LLMError(f"claude-cli exit {p.returncode} ({data.get('subtype', 'no json')}): {detail}")
        text = str(data.get("result") or "")
        usage = data.get("usage") or {}
        data = {**data, "model": self.model,
                "usage": {"input_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                          + int(usage.get("cache_creation_input_tokens") or 0),
                          "output_tokens": int(usage.get("output_tokens") or 0)}}
        return text, data

    def _request(self, system: str, user: str, max_tokens: int, temperature: float) -> tuple[str, dict]:
        if self.provider == "claude-cli":
            return self._request_cli(system, user)
        headers = {"content-type": "application/json"}
        if self.provider == "anthropic":
            url = f"{self.base_url}/v1/messages"
            headers.update({"x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
            body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                                    "system": system, "messages": [{"role": "user", "content": user}]}
        else:
            url = f"{self.base_url}/chat/completions"
            if self.api_key:
                headers["authorization"] = f"Bearer {self.api_key}"
            body = {"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:400] if hasattr(e, "read") else ""
            raise LLMError(f"HTTP {e.code} from {self.provider}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise LLMError(f"{self.provider}: {e}") from e
        if self.provider == "anthropic":
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        else:
            choices = data.get("choices") or []
            text = (choices[0].get("message") or {}).get("content", "") if choices else ""
            if not text and data.get("error"):
                raise LLMError(f"openrouter: {data['error']}")
        return text or "", data

    def complete(self, system: str, user: str, max_tokens: int = 4096, temperature: float = 0.0) -> Completion:
        attempt, delay = 0, 0.5
        while True:
            t0 = time.monotonic()
            try:
                text, data = self._request(system, user, max_tokens, temperature)
                break
            except LLMError as e:
                if attempt >= self.max_retries or "HTTP 4" in str(e) and "429" not in str(e):
                    raise
                attempt += 1
                time.sleep(delay)
                delay *= 2
        usage = data.get("usage") or {}
        return Completion(text=text, latency_ms=int((time.monotonic() - t0) * 1000), model=str(data.get("model", self.model)),
                          input_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                          output_tokens=int(usage.get("output_tokens") or usage.get("completion_tokens") or 0), raw=data)

    def complete_json(self, system: str, user: str, max_tokens: int = 4096) -> tuple[dict, Completion]:
        """Ask for JSON; parse the first balanced object in the reply; one repair retry."""
        comp = self.complete(system, user, max_tokens)
        parsed = extract_json(comp.text)
        if parsed is None:
            comp2 = self.complete(system, user + "\n\nYour previous reply was not valid JSON. Reply with only the JSON object.", max_tokens)
            parsed = extract_json(comp2.text)
            comp = Completion(comp2.text, comp.latency_ms + comp2.latency_ms, comp2.model,
                              comp.input_tokens + comp2.input_tokens, comp.output_tokens + comp2.output_tokens, comp2.raw)
            if parsed is None:
                raise LLMError("model did not return JSON: " + comp.text[:200])
        return parsed, comp


def extract_json(text: str) -> dict | None:
    """Find the first balanced top-level JSON object in ``text`` (tolerates code fences)."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        return obj if isinstance(obj, dict) else None
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return None


def make_llm(section: dict[str, Any]) -> LLMClient:
    return LLMClient(section.get("provider", "auto"), section.get("model", "claude-haiku-5-5"),
                     int(section.get("timeout_ms", 30000)), base_url=section.get("base_url"), command=section.get("command"))
