"""The monitor's own files: notice when the agent writes them, and when the enforcement config changes.

The agent has Write and Bash, so it can switch its own monitor off: ``{"gate": {"tools": []}}`` in
``.claude/epistemic.json``, ``enforce: false`` in the data dir's ``config.json``, or a forged event replayed into
``index.db`` (review 2026-11-19, idea B; ClawMMU arXiv 2610.28093: authority never lifts itself; state-transfer
auditing arXiv 2611.23346: unverified memory writes). A deny cannot close this: shell bypasses are unbounded and
every hook fails open. So this module makes it visible instead, never denies:

- ``protected_target``: a Write/Edit/MultiEdit/NotebookEdit or a writing Bash command aimed at the data dir, a
  config layer, or a Claude Code settings file that touches hooks or ``EPISTEMIC_*``. The gate adds an advisory and
  logs ``monitor_tamper``.
- ``config_change``: a fingerprint of the merged config, kept per project. When it differs from the last one seen
  (whoever changed it, by whatever route), the user is told once which keys changed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

from . import authority
from .config import Config, claude_config_dir

PROJECT_STATE = "_project"
FP_KEY = "config_fingerprint"

_READ_ONLY_LEAD = re.compile(r"^\s*(?:cat|less|more|head|tail|grep|rg|ls|wc|stat|file|diff|jq|sqlite3\s+-readonly|"
                             r"em(?:-hook)?\s+ledger\s+(?:status|list|show|why|search))\b")
_WRITE_HINT = re.compile(r">|\b(?:tee|sed|perl|rm|mv|cp|rsync|install|ln|touch|truncate|dd|sqlite3|python3?|node|"
                         r"ruby|echo|printf|chmod|unlink)\b")
_CONTROL = re.compile(r"[;&|`\n]|\$\(")
_EPISTEMIC_PATH = re.compile(r"epistemic\.json|/epistemic(?:/|\b|$)")
_SETTINGS_PATH = re.compile(r"\.claude/settings(?:\.local)?\.json|\bsettings(?:\.local)?\.json\b")
_SETTINGS_RISK = re.compile(r"disableAllHooks|\"hooks\"|EPISTEMIC_|epistemic")


def _real(p: Path) -> str:
    try:
        return os.path.realpath(p.expanduser())
    except (OSError, ValueError):
        return str(p)


def _config_files(cfg: Config) -> dict[str, str]:
    ccd = claude_config_dir()
    return {_real(cfg.cwd / ".claude" / "epistemic.json"): "project config (.claude/epistemic.json)",
            _real(ccd / "epistemic.json"): "user config (~/.claude/epistemic.json)"}


def _settings_files(cfg: Config) -> set[str]:
    ccd = claude_config_dir()
    return {_real(cfg.cwd / ".claude" / "settings.json"), _real(cfg.cwd / ".claude" / "settings.local.json"),
            _real(ccd / "settings.json"), _real(ccd / "settings.local.json")}


def protected_target(tool: str, tool_input: dict[str, Any], cfg: Config) -> str | None:
    """What of the monitor's own state this call writes, or None. Reads never count."""
    ti = tool_input if isinstance(tool_input, dict) else {}
    if tool == "Bash":
        cmd = str(ti.get("command") or "")
        if not cmd.strip() or authority.needs_check(cmd):
            return None   # ledger CLI with user authority: authority.py decides
        if _READ_ONLY_LEAD.match(cmd) and not _CONTROL.search(cmd) and ">" not in cmd:
            return None
        if not _WRITE_HINT.search(cmd):
            return None
        data = _real(cfg.data_dir)
        if _EPISTEMIC_PATH.search(cmd) or data in cmd or str(cfg.data_dir) in cmd:
            return "the monitor's ledger or config"
        if _SETTINGS_PATH.search(cmd) and _SETTINGS_RISK.search(cmd):
            return "Claude Code settings (hooks or EPISTEMIC_* env)"
        return None
    raw = str(ti.get("file_path") or ti.get("notebook_path") or "")
    if not raw:
        return None
    p = Path(raw).expanduser()
    real = _real(p if p.is_absolute() else cfg.cwd / p)
    data = _real(cfg.data_dir)
    if real == data or real.startswith(data.rstrip(os.sep) + os.sep):
        return f"the monitor's data directory ({Path(real).name})"
    configs = _config_files(cfg)
    if real in configs:
        return configs[real]
    if real in _settings_files(cfg):
        text = json.dumps(ti)
        if _SETTINGS_RISK.search(text):
            return "Claude Code settings (hooks or EPISTEMIC_* env)"
    return None


def advisory_text(what: str) -> str:
    return (f"Epistemic monitor: this call writes {what}. Changing the monitor's ledger or configuration is the "
            "user's decision: do it only if the user asked for it in this conversation, and say so in your reply. "
            "The user is notified of every change to the monitor's configuration.")


def _flatten(d: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(d, dict):
        return {prefix: d}
    out: dict[str, Any] = {}
    for k, v in d.items():
        out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    return out


def config_change(cfg: Config, store) -> list[str] | None:
    """Dotted keys whose merged value changed since the last check in this project (``a.b (old -> new)``), or None
    when nothing changed or there is no earlier fingerprint. Records the current fingerprint either way."""
    flat = {k: v for k, v in _flatten(cfg.values).items()}
    blob = json.dumps(flat, sort_keys=True, default=str)
    fp = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
    prev = store.get_state(PROJECT_STATE, FP_KEY) or {}
    if prev.get("fp") == fp:
        return None
    store.set_state(PROJECT_STATE, FP_KEY, {"fp": fp, "values": flat})
    if not prev:
        return None
    old = prev.get("values") or {}
    keys = sorted(set(old) | set(flat))
    return [f"{k} ({json.dumps(old.get(k), default=str)} -> {json.dumps(flat.get(k), default=str)})"
            for k in keys if old.get(k) != flat.get(k)]


def change_notice(changed: list[str], cfg: Config) -> str:
    shown = ", ".join(changed[:8]) + (f", and {len(changed) - 8} more" if len(changed) > 8 else "")
    return ("Epistemic monitor: its configuration changed since it last ran: " + shown + ". If you did not make this "
            "change, check .claude/epistemic.json, ~/.claude/epistemic.json, "
            f"{cfg.data_dir / 'config.json'} and EPISTEMIC_* environment variables.")
