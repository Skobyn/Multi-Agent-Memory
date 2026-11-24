"""Credentials are not stored in the ledger (RESEARCH-2026-11-24, after FORGETMENOT 2611.29804 and HEAMS 2611.20144)."""
import pytest
from fakes import FakeLLM
from helpers import make_entry

from epistemic_monitor.anchors import extract_anchors
from epistemic_monitor.extractor import Extractor
from epistemic_monitor.pinning import render_pinned
from epistemic_monitor.redact import redact
from epistemic_monitor.transcript import KIND_USER_TEXT, Turn

# built from parts so the test file itself trips no secret scanner
KEY = "sk-ant-api03-" + "Ab3dEf6hIj9kLm2nOp5qRs8tUv1wXy4z" + "Q7"
URL = "postgres://admin:" + "hunter2pass" + "@db:5432/app"
AWS = "AK" + "IA" + "Q" * 16
PEM = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\n-----END RSA " + "PRIVATE KEY-----"

SECRETS = [KEY, "ghp_" + "a1B2c3D4" * 5, AWS, "xoxb-" + "1234567890-abcdef", "AIza" + "S" * 35,
           "eyJhbGciOiJIUzI1" + ".eyJzdWIiOiIxMjM0.SflKxwRJSMeKKF2QT4fw", PEM,
           "sk-proj-" + "abcdEFGHijklMNOPqrstUVWX1234abcd"]

KEPT = ["commit 3f9a2b1c4d5e6f708192a3b4c5d6e7f8091a2b3c must stay", "UUID 123e4567-e89b-12d3-a456-426614174000",
        "sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "src/epistemic_monitor/extractor.py",
        "integrity sha512-z4PhNX7vuL3xVChQ1m2AB9Yg5AULVxXcg/SpIdNs6c5H0NE8XYXysP+DGNKHfuwvY7kxvUdBeoGlODJ6+SfaPg==",
        "the token= parameter is required", "password: must be hashed with bcrypt", "token: ${GITHUB_TOKEN}",
        "set API_KEY=$ANTHROPIC_API_KEY in CI", "http://localhost:8000/v1", "git@github.com:org/repo.git",
        "ssh://git@host:22/repo", "keep max_tokens: 4096", "pass --token-file secrets/tok",
        "Secret: rotateWeekly policy applies", "sk-scikit-learn-compat-wrapper-module", "password=${DB_PASSWORD}"]


@pytest.mark.parametrize("secret", SECRETS)
def test_known_credential_shapes_are_redacted(secret):
    out = redact(f"use {secret} for now")
    assert secret not in out and "[REDACTED:" in out


def test_url_password_and_assignment_keep_their_context():
    assert redact(URL) == "postgres://[REDACTED:url-credentials]@db:5432/app"
    assert redact("password=" + "hunter2pass") == "password=[REDACTED:assignment]"


@pytest.mark.parametrize("text", KEPT)
def test_shas_paths_and_placeholders_are_left_alone(text):
    assert redact(text) == text


def test_placeholder_is_not_an_anchor():
    assert extract_anchors("never commit [REDACTED:api-key]") == []


def test_extractor_stores_no_secret_and_keeps_user_authority(store):
    prompt = f"Use {KEY} for the staging API and never commit it. The db is {URL}."
    turns = [Turn(uuid="u-1", kind=KIND_USER_TEXT, role="user", text=prompt)]
    resp = {"entries": [
        {"type": "constraint", "statement": f"Never commit the key {KEY}.", "rationale": f"user gave {KEY}",
         "source": "user", "evidence": f"Use {KEY} for the staging API and never commit it", "turn_uuid": "u-1",
         "falsifiers": [{"kind": "diff_regex_forbidden", "pattern": KEY}], "confidence": 0.95},
        {"type": "premise", "statement": f"The database is {URL}.", "source": "user",
         "evidence": f"The db is {URL}", "turn_uuid": "u-1", "confidence": 0.9},
    ], "supersedes": []}
    res = Extractor(store, FakeLLM([resp]), None).run(turns, "s1", mode="incremental")
    assert len(res.added) == 2
    c, p = store.get_entry("C-0001"), store.get_entry("P-0001")
    for e in (c, p):
        # the evidence checks ran on the raw quote: still user authority, not tagged unverified
        assert e.provenance.source == "user" and not e.evidence_unverified
        blob = " ".join([e.statement, e.rationale, e.evidence, *e.anchors])
        assert KEY not in blob and "hunter2pass" not in blob and "REDACTED" not in e.anchors
    assert c.falsifiers == [{"kind": "diff_regex_forbidden", "pattern": KEY}]   # the enforcement keeps the literal
    pinned = render_pinned(store.active_entries())
    assert KEY not in pinned and "hunter2pass" not in pinned
    events = str(store.iter_events())
    assert events.count(KEY) == 1 and "hunter2pass" not in events    # only inside the falsifier pattern


def test_supersede_reason_is_redacted(store):
    make_entry(store, source="agent", confirmed=False)
    assert store.supersede("D-0001", None, f"rotated to {KEY}", by="agent")
    assert KEY not in store.iter_events()[-1]["reason"]


ENV = ["DB_PASSWORD=" + "abc12345678", "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
       "SECRET_KEY=" + "django-insecure-abc123", "api_key = '" + "x9y8z7w6v5u4" + "'",
       'password="' + "hunter2hunter" + '"', "Authorization: Bearer " + "abcDEF0123456789ghiJKL",
       "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc", "glpat-" + "xxxxxxxxxxxxxxxxxxxx1", "hf_" + "a1" * 17]


@pytest.mark.parametrize("text", ENV)
def test_env_style_and_other_common_secrets_are_redacted(text):
    assert "[REDACTED:" in redact(text)


@pytest.mark.parametrize("text", ["pwd=/home/user/project2", "passwd=/etc/passwd2",
                                  "branch sk-1234-refactor-payment-service-module-v2-final",
                                  "task-sk-" + "0123456789abcdef" * 2 + ".md",
                                  "https://a.com/x?redirect=https://b.com:443@c"])
def test_review_false_positives_are_left_alone(text):
    assert redact(text) == text


def test_bare_private_key_header_keeps_the_rule():
    text = "Never commit files containing '-----BEGIN RSA " + "PRIVATE KEY-----'; pre-commit scans for it in CI"
    assert redact(text) == text


def test_redaction_stays_linear_on_adversarial_input():
    import time
    for s in ["sk-" * 70000, ("my-sk-" + "a" * 10) * 3000, "://" + "a:" * 50000, "password=" + "1" * 200000,
              "-----BEGIN RSA " + "PRIVATE KEY-----" * 10000]:
        t = time.monotonic()
        redact(s)
        assert time.monotonic() - t < 2.0


def test_user_supersede_line_reason_is_logged_redacted(store):
    from epistemic_monitor.hooks import _apply_supersede_lines
    make_entry(store)
    _apply_supersede_lines(f"SUPERSEDE D-0001: rotate to {KEY}", "user", store, "s1")
    assert KEY not in (store.config.data_dir / "interventions.jsonl").read_text()
