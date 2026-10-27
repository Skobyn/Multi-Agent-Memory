from helpers import FIXTURES

from epistemic_monitor.transcript import (KIND_ASSISTANT_TEXT, KIND_COMPACT_BOUNDARY, KIND_COMPACT_SUMMARY,
                                          KIND_TOOL_RESULT, KIND_TOOL_USE, KIND_USER_TEXT,
                                          capture_transcript, read_turns)

REAL = FIXTURES / "transcript_claude_code_2.1.311.jsonl"
OLD = FIXTURES / "transcript_synthetic_older_format.jsonl"


def test_real_fixture_parses_without_errors():
    turns, stats = read_turns(REAL)
    assert stats.errors == 0
    assert stats.versions == {"2.1.311"}
    kinds = {t.kind for t in turns}
    assert {KIND_USER_TEXT, KIND_ASSISTANT_TEXT, KIND_TOOL_USE, KIND_TOOL_RESULT} <= kinds
    first_user = next(t for t in turns if t.kind == KIND_USER_TEXT)
    assert first_user.session_id and first_user.uuid and first_user.text
    tool = next(t for t in turns if t.kind == KIND_TOOL_USE)
    assert tool.tool_name == "Bash" and "command" in tool.tool_input and tool.tool_use_id.startswith("toolu_")


def test_older_format_fixture_detects_compaction_and_skips_noise():
    turns, stats = read_turns(OLD)
    assert stats.errors == 1  # the garbage line
    assert stats.compactions == 1
    assert stats.skipped_sidechain == 1
    kinds = [t.kind for t in turns]
    assert kinds.count(KIND_COMPACT_SUMMARY) == 2  # old 'summary' row + isCompactSummary row
    boundary = next(t for t in turns if t.kind == KIND_COMPACT_BOUNDARY)
    assert boundary.extra["trigger"] == "auto" and boundary.extra["pre_tokens"] == 171234
    assert not any("subagent chatter" in t.text for t in turns)
    bash = [t for t in turns if t.kind == KIND_TOOL_USE and t.tool_name == "Bash"]
    assert bash and bash[0].tool_input["command"] == "pip install pymongo"


def test_watermark_yields_only_newer_rows():
    turns, _ = read_turns(OLD, after_uuid="u-003")
    uuids = [t.uuid for t in turns]
    assert "u-001" not in uuids and "a-002" not in uuids
    assert uuids[0] == "c-005"


def test_unknown_watermark_replays_everything():
    all_turns, _ = read_turns(OLD)
    turns, _ = read_turns(OLD, after_uuid="never-seen")
    assert len(turns) == len(all_turns)


def test_capture_round_trip(tmp_path):
    out = capture_transcript(OLD, tmp_path / "t", "label")
    assert out.exists() and out.suffix == ".gz"
    turns, stats = read_turns(out)
    assert stats.compactions == 1 and len(turns) > 5


def test_capture_missing_source_is_harmless(tmp_path):
    out = capture_transcript(tmp_path / "nope.jsonl", tmp_path / "t", "x")
    assert not out.exists()
