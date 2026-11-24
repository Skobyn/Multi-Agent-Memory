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
    # a whole block, or a header followed by key body lines; a bare header ("files containing -----BEGIN ...")
    # is not a secret and stays, with the rest of the sentence (review of the diff, 2026-11-24)
    ("private-key", re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----"
                               r"(?:[\s\S]{0,20000}?-----END [A-Z ]{0,40}PRIVATE KEY-----|(?:\s{1,4}[A-Za-z0-9+/=]{40,})+)")),
    # sk-, sk-ant-, sk-proj-, sk-svcacct-: 32+ characters with a digit and a 20-character run without a hyphen
    # (`sk-scikit-learn-compat-wrapper`, `sk-1234-refactor-payment-...` are names). Lookaheads are bounded so a long
    # run of `sk-` stays linear.
    ("api-key", re.compile(r"(?<![\w-])sk-(?:ant-|proj-|svcacct-)?(?=[A-Za-z0-9_-]{0,200}\d)"
                           r"(?=[A-Za-z0-9_-]{0,200}[A-Za-z0-9_]{20})[A-Za-z0-9_-]{32,400}")),
    ("stripe-key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,200}")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,200}|github_pat_[A-Za-z0-9_]{40,300})")),
    ("gitlab-token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,200}")),
    ("huggingface-token", re.compile(r"\bhf_[A-Za-z0-9]{30,200}")),
    ("npm-token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("aws-key-id", re.compile(r"\b(?:AK|AS)IA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,200}")),
    ("google-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[\w-]{8,2000}\.eyJ[\w-]{8,2000}\.[\w-]{8,2000}")),
    ("bearer-token", re.compile(r"(?<=\bBearer )[A-Za-z0-9._~+/-]{20,2000}=*")),
    # user:password@ in a URL (git@host and ssh://git@host have no password part; host:443@ is a port)
    ("url-credentials", re.compile(r"(?<=://)[^\s/:@]{1,200}:(?!\d{1,5}@)[^\s/@]{1,200}(?=@)")),
]
# NAME=value, including env-style names (DB_PASSWORD=, AWS_SECRET_ACCESS_KEY=, api_key = '...'): a value with a
# digit, not a path, placeholder or `$VAR` (`token= parameter`, `pwd=/home/u/p2`, `password=${X}` stay)
_ASSIGN = re.compile(r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_]{0,40}?(?:password|passwd|pwd|secret|token|api[_-]?key|"
                     r"access[_-]?key|client[_-]?secret)[A-Za-z0-9_]{0,40} ?= ?['\"]?)"
                     r"(?![\[<{$/~.])(?=[^\s'\"`,;]{0,200}\d)[^\s'\"`,;]{8,400}")


def redact(text: str) -> str:
    """``text`` with each credential replaced by ``[REDACTED:<kind>]``."""
    if not text:
        return text
    for kind, pat in _PATTERNS:
        text = pat.sub(f"[REDACTED:{kind}]", text)
    return _ASSIGN.sub(lambda m: m.group(1) + "[REDACTED:assignment]", text)
