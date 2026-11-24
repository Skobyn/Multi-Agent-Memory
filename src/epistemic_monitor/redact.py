"""Remove credentials from text before the ledger stores it (RESEARCH-2026-11-24).

A user often types a rule and a secret in one prompt ("use sk-ant-... and never commit it"). Whatever the extractor
stores in ``statement``, ``evidence`` or ``rationale`` is re-shown after every compaction (pinned.md), in every
subagent, in the MEMORY.md mirror and in deny citations, and the statement goes to the decision model and the
embeddings provider on later calls (FORGETMENOT 2611.29804; HEAMS 2611.20144). The redaction runs once, when an
entry is built, so every copy downstream is clean. Evidence checks run on the raw text first.

Only well-known credential shapes are matched. There is no entropy rule: one strict enough to catch keys also
catches commit SHAs and integrity hashes, which are real anchors. Falsifiers are never redacted: a "never commit
this key" falsifier that holds the literal key is the enforcement, and falsifiers are not rendered or sent out.
The raw prompt still reaches the extractor model, and ``transcripts/`` captures keep the verbatim transcript.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)")),
    # sk-, sk-ant-, sk-proj-, sk-svcacct-: 32+ characters with a digit (`sk-scikit-learn-compat-wrapper` is a name)
    ("api-key", re.compile(r"\bsk-(?:ant-|proj-|svcacct-)?(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{32,}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})")),
    ("aws-key-id", re.compile(r"\b(?:AK|AS)IA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[\w-]{8,}\.eyJ[\w-]{8,}\.[\w-]{8,}")),
    # user:password@ in a URL (git@host and ssh://git@host have no password part)
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]+:[^\s/@]+(?=@)")),
]
# name=value with no space and a value holding a digit: `password=hunter2pass`, not `token= parameter` or `$VAR`
_ASSIGN = re.compile(r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|client[_-]?secret)=)"
                     r"(?![\[<{$])(?=[^\s'\"`,;]*\d)[^\s'\"`,;]{8,}")


def redact(text: str) -> str:
    """``text`` with each credential replaced by ``[REDACTED:<kind>]``."""
    if not text:
        return text
    for kind, pat in _PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    return _ASSIGN.sub(lambda m: m.group(1) + "[REDACTED:assignment]", text)
