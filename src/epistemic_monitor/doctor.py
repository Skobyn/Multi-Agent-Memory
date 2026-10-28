"""`em-hook doctor`: one real call per configured component, so a user can see that the keys and
providers work before trusting the gate to a session. Prints one line per check; exit 1 if any
required check fails (the decision model and the advisor; the extractor and embeddings warn only)."""
from __future__ import annotations

import os
import sys
import time
from typing import Any

from . import __version__
from .config import Config, load_config

KEY_VARS = ("TYPESAFE_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


def _mask(v: str) -> str:
    return (v[:4] + "…" + v[-3:]) if len(v) > 10 else "set"


def _timed(fn):
    t0 = time.monotonic()
    out = fn()
    return out, int((time.monotonic() - t0) * 1000)


def run_checks(cfg: Config) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool | None, detail: str, required: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "required": required})

    add("python", sys.version_info >= (3, 10), f"{sys.version.split()[0]}")
    add("data dir", True, str(cfg.data_dir), required=False)
    present = {k: _mask(os.environ[k]) for k in KEY_VARS if os.environ.get(k)}
    add("keys in environment", None, ", ".join(f"{k}={v}" for k, v in present.items()) or "none (a proxy or the CLI's own sign-in may still serve the calls)", required=False)

    # decision model: one Noul question
    dm_cfg = cfg.section("decision_model")
    label = f"decision model {dm_cfg.get('provider')}:{dm_cfg.get('model')}"
    try:
        from .decision_model import make_decision_model
        dm = make_decision_model(dm_cfg, cfg.section("extractor"))
        r, ms = _timed(lambda: dm.noul("Is the statement about a database?", {"statement": "Use PostgreSQL as the primary database."},
                                       {"true": "It names or concerns a database.", "false": "It does not."}))
        add(label, r.p_true >= 0.5, f"p={r.p_true:.2f} in {ms} ms (expected p ≥ 0.5)")
    except Exception as e:  # noqa: BLE001
        add(label, False, f"{type(e).__name__}: {str(e)[:160]}")

    # advisor and extractor: one tiny completion each
    for role, required in (("advisor", True), ("extractor", False)):
        sec = cfg.section(role)
        label = f"{role} {sec.get('provider')}:{sec.get('model')}"
        try:
            from .llm import make_llm
            llm = make_llm(sec)
            comp, ms = _timed(lambda: llm.complete("Reply with exactly the word OK and nothing else.", "Ready?", max_tokens=5))
            ok = "ok" in comp.text.lower()
            add(f"{label} (resolved {llm.name})", ok, f"{comp.text.strip()[:30]!r} in {ms} ms", required=required)
        except Exception as e:  # noqa: BLE001
            add(label, False, f"{type(e).__name__}: {str(e)[:160]}", required=required)

    # embeddings
    emb = cfg.section("embeddings")
    label = f"embeddings {emb.get('provider')}:{emb.get('model')}"
    if emb.get("provider") == "none":
        add(label, None, "disabled (scope + keyword retrieval only)", required=False)
    else:
        try:
            from .embeddings import make_embedder
            vec, ms = _timed(lambda: make_embedder(emb).embed(["ping"])[0])
            add(label, bool(vec), f"{len(vec)} dims in {ms} ms", required=False)
        except Exception as e:  # noqa: BLE001
            add(label, False, f"{type(e).__name__}: {str(e)[:160]}", required=False)
    add("gate", bool(cfg.get("gate", "enforce", True)), "enforce=" + str(cfg.get("gate", "enforce", True)) +
        f", deny_budget={cfg.get('gate', 'deny_budget', 5)}", required=False)
    return checks


def main(argv: list[str] | None = None) -> int:
    cfg = load_config()
    print(f"epistemic-monitor {__version__} doctor")
    checks = run_checks(cfg)
    failed = False
    for c in checks:
        mark = "PASS" if c["ok"] else ("FAIL" if c["ok"] is False else "INFO")
        if c["ok"] is False and c["required"]:
            failed = True
        elif c["ok"] is False:
            mark = "WARN"
        print(f"  {mark:4} {c['name']}: {c['detail']}")
    print("RESULT:", "FAIL (a required component is not working)" if failed else "PASS")
    return 1 if failed else 0
