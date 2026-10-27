from epistemic_monitor.anchors import extract_anchors


def test_anchor_kinds():
    a = extract_anchors("Use PostgreSQL as the primary database.",
                        "config lives in src/db/client.py and reads DATABASE_URL=postgres://db/app; see #42 and "
                        "https://example.com/docs/x. Commit 9f8e7d6c5b4a fixed the TimeoutError on 10.0.0.12.")
    assert "src/db/client.py" in a
    assert "PostgreSQL" in a
    assert "#42" in a
    assert "https://example.com/docs/x" in a
    assert "TimeoutError" in a
    assert "10.0.0.12" in a
    assert any(x.startswith("DATABASE_URL=") for x in a)
    assert "9f8e7d6c5b4a" in a
    assert "Use" not in a and "The" not in a


def test_anchor_dedup_and_terms_limit():
    a = extract_anchors("Run pytest before every commit. Run pytest again.", max_terms=3)
    assert a.count("pytest") <= 1
    assert len(a) <= 3
