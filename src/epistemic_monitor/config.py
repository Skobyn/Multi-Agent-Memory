"""Configuration (DESIGN.md §12) and on-disk layout (DESIGN.md §2).

Precedence, lowest to highest: built-in defaults, the repo's
``.claude/epistemic.json``, the per-project ``config.json`` in the data
directory, then ``EPISTEMIC_<SECTION>_<KEY>`` environment variables.
Secrets never live in config files; they come from ``TYPESAFE_API_KEY``,
``ANTHROPIC_API_KEY`` and ``OPENROUTER_API_KEY``.
"""

from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, dict[str, Any]] = {
    "decision_model": {
        "provider": "typesafe",  # typesafe | llm
        "model": "jev-latest",
        "timeout_ms": 1500,
        "max_rps": 20,
        "max_question_tokens": 32000,
        "fallback": "llm",  # llm | skip
        "base_url": "https://api.typesafe.ai",
    },
    "extractor": {"provider": "anthropic", "model": "claude-haiku-5-5", "timeout_ms": 30000},
    "advisor": {"provider": "anthropic", "model": "claude-opus-5-5", "timeout_ms": 20000},
    "embeddings": {
        "provider": "openrouter",  # openrouter | local | none
        "model": "openai/text-embedding-3-small",
        "base_url": "https://openrouter.ai/api/v1",
        "timeout_ms": 10000,
    },
    "gate": {
        "enforce": False,  # Phase 0/1: advisory only. Phase 2 flips this on.
        "tau_default": 0.75,
        "tau_by_type": {"process": 0.6, "commitment": 0.6},
        "deny_budget": 5,
        "false_deny_alpha": 0.05,
        "cooldown_steps": 5,
        "max_consecutive_denies": 5,
        "authorization_shaped_to_advisor": True,
        "tools": ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"],
    },
    "pinning": {"max_entries": 25, "max_chars": 10000, "mirror_memory_md": True},
    "audit": {"behavior_watch_batches": 3, "entailment_check": True},
    "extraction": {"incremental": True, "dedup_cosine": 0.92},
    "logging": {"shadow_mode": True, "level": "info"},
}

_ENV_PREFIX = "EPISTEMIC_"


def project_slug(cwd: str | os.PathLike[str]) -> str:
    """Replicate Claude Code's project directory slug: non-alphanumerics become '-'.

    ``/home/user/Multi-Agent-Memory`` -> ``-home-user-Multi-Agent-Memory``.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def claude_config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def data_dir_for(cwd: str | os.PathLike[str]) -> Path:
    """Where this project's ledger lives (DESIGN.md §2)."""
    override = os.environ.get(_ENV_PREFIX + "DATA_DIR")
    if override:
        return Path(override).expanduser()
    return claude_config_dir() / "projects" / project_slug(cwd) / "epistemic"


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _load_json_file(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _coerce(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def _env_overrides(environ: dict[str, str]) -> dict:
    """``EPISTEMIC_GATE_TAU_DEFAULT=0.8`` -> ``{"gate": {"tau_default": 0.8}}``.

    The section is the first token after the prefix; the rest is the key.
    Keys are matched case-insensitively against known defaults so that
    multi-word keys (``MAX_RPS``) resolve correctly.
    """
    out: dict = {}
    for name, raw in environ.items():
        if not name.startswith(_ENV_PREFIX) or name == _ENV_PREFIX + "DATA_DIR":
            continue
        rest = name[len(_ENV_PREFIX):].lower()
        # Longest section prefix wins so ``decision_model_max_rps`` resolves correctly.
        section = next((s for s in sorted(DEFAULTS, key=len, reverse=True) if rest.startswith(s + "_")), None)
        if section is None:
            continue
        key = rest[len(section) + 1:]
        if not key:
            continue
        known = {k.lower(): k for k in DEFAULTS[section]}
        out.setdefault(section, {})[known.get(key, key)] = _coerce(raw)
    return out


@dataclass
class Config:
    values: dict[str, dict[str, Any]]
    cwd: Path
    data_dir: Path

    # ---- paths (DESIGN.md §2) ----
    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.jsonl"

    @property
    def index_path(self) -> Path:
        return self.data_dir / "index.db"

    @property
    def interventions_path(self) -> Path:
        return self.data_dir / "interventions.jsonl"

    @property
    def pinned_path(self) -> Path:
        return self.data_dir / "pinned.md"

    @property
    def calibration_path(self) -> Path:
        return self.data_dir / "calibration.json"

    @property
    def transcripts_dir(self) -> Path:
        return self.data_dir / "transcripts"

    @property
    def memory_md_path(self) -> Path:
        return self.data_dir.parent / "memory" / "MEMORY.md"

    def section(self, name: str) -> dict[str, Any]:
        return self.values[name]

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.values.get(section, {}).get(key, default)

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)


def load_config(cwd: str | os.PathLike[str] | None = None,
                environ: dict[str, str] | None = None) -> Config:
    env = dict(os.environ if environ is None else environ)
    cwd_path = Path(cwd or env.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
    data_dir = data_dir_for(cwd_path) if environ is None else (
        Path(env[_ENV_PREFIX + "DATA_DIR"]).expanduser() if _ENV_PREFIX + "DATA_DIR" in env
        else claude_config_dir() / "projects" / project_slug(cwd_path) / "epistemic")
    values = copy.deepcopy(DEFAULTS)
    values = _deep_merge(values, _load_json_file(cwd_path / ".claude" / "epistemic.json"))
    values = _deep_merge(values, _load_json_file(data_dir / "config.json"))
    values = _deep_merge(values, _env_overrides(env))
    return Config(values=values, cwd=cwd_path, data_dir=data_dir)
