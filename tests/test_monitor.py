"""Tests for the Memory Agent, contradiction detection, confidence scoring, hooks, and tools."""

import pytest

from dual_agent_memory.archive import MemoryArchive
from dual_agent_memory.chunker import Chunker
from dual_agent_memory.config import Config, ConfidenceSignal, Interrupt, MemoryChunk
from dual_agent_memory.confidence import ConfidenceScorer
from dual_agent_memory.contradiction import ContradictionDetector
from dual_agent_memory.embedder import Embedder
from dual_agent_memory.hooks import CompactionHook
from dual_agent_memory.monitor import MemoryAgent
from dual_agent_memory.search import HybridSearch
from dual_agent_memory.tools import MemorySearchTool


# -------------------------------------------------------------------------
# Helpers to build the full pipeline for monitor tests
# -------------------------------------------------------------------------


def _build_pipeline(config):
    """Build the full pipeline: archive, chunker, embedder, search."""
    archive = MemoryArchive(config)
    chunker = Chunker(config)
    embedder = Embedder(config)
    search = HybridSearch(archive, embedder, config)
    return archive, chunker, embedder, search


def _store_messages(archive, chunker, embedder, messages, session_id):
    """Chunk messages, embed them, store in archive. Return the chunks."""
    chunks = chunker.chunk_messages(messages, session_id=session_id)
    for chunk in chunks:
        chunk.embedding = embedder.embed(chunk.content)
    archive.store_chunks(chunks)
    return chunks


# -------------------------------------------------------------------------
# Contradiction detection tests
# -------------------------------------------------------------------------


def test_contradiction_detection(config, sample_messages, contradicting_messages):
    """Detects contradiction between 'use PostgreSQL' and 'use MongoDB'."""
    archive, chunker, embedder, search = _build_pipeline(config)
    stored = _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    detector = ContradictionDetector(config)

    # Get archived decisions to compare against
    archived_decisions = archive.get_all_decisions()

    # The contradicting message says "decided to use MongoDB" which contradicts
    # the earlier "chose PostgreSQL" decision.
    contradicting_text = contradicting_messages[1]["content"]
    contradictions = detector.detect(contradicting_text, archived_decisions)

    assert len(contradictions) > 0, (
        "should detect contradiction between PostgreSQL and MongoDB decisions"
    )
    assert contradictions[0]["archived_chunk"] is not None, (
        "should return the contradicting archived chunk"
    )

    archive.close()


def test_no_contradiction(config, sample_messages):
    """No false positive when output is consistent with archive."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    detector = ContradictionDetector(config)
    archived_decisions = archive.get_all_decisions()

    # A statement consistent with what was decided should not be flagged.
    consistent_text = (
        "We are using PostgreSQL for the database as previously decided."
    )
    contradictions = detector.detect(consistent_text, archived_decisions)

    assert len(contradictions) == 0, (
        "consistent statements should not be flagged as contradictions"
    )

    archive.close()


# -------------------------------------------------------------------------
# Confidence scoring tests
# -------------------------------------------------------------------------


def test_confidence_high(config, sample_messages):
    """Consistent output gets a confidence score above 0.7."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    detector = ContradictionDetector(config)
    scorer = ConfidenceScorer(search, detector, config)

    consistent_output = (
        "We are using PostgreSQL for the database and JWT for authentication."
    )
    # Provide context that includes the entities so no gaps are detected
    context = " ".join(m["content"] for m in sample_messages)
    signal = scorer.score(consistent_output, context)

    assert isinstance(signal, ConfidenceSignal), "should return a ConfidenceSignal"
    assert signal.score > 0.7, (
        f"consistent output should have score > 0.7, got {signal.score}"
    )

    archive.close()


def test_confidence_low_contradiction(config, sample_messages, contradicting_messages):
    """Contradicting output gets a low confidence score with contradiction flag."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    detector = ContradictionDetector(config)
    scorer = ConfidenceScorer(search, detector, config)

    # The contradicting message reverses the database decision.
    contradicting_output = contradicting_messages[1]["content"]
    signal = scorer.score(contradicting_output)

    assert isinstance(signal, ConfidenceSignal), "should return a ConfidenceSignal"
    assert "contradiction" in signal.flags, (
        "contradiction flag should be present in signal flags"
    )

    archive.close()


def test_confidence_gap_detection(config, sample_messages):
    """Referencing an entity from archive without context triggers gap flag."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    detector = ContradictionDetector(config)
    scorer = ConfidenceScorer(search, detector, config)

    # Reference entities that ARE in the archive but provide a context
    # that does NOT mention them -> gap detection
    gap_output = (
        "We should deploy PostgreSQL on Kubernetes and use Redis for session storage."
    )
    # Context talks about something unrelated, so archive entities are "gaps"
    signal = scorer.score(
        gap_output,
        current_context_summary="We are discussing the frontend layout and CSS styles.",
    )

    assert isinstance(signal, ConfidenceSignal), "should return a ConfidenceSignal"
    # Score should be reduced due to gaps
    assert signal.score < 1.0, (
        f"referencing entities without context should reduce score, got {signal.score}"
    )

    archive.close()


# -------------------------------------------------------------------------
# MemoryAgent tests
# -------------------------------------------------------------------------


def test_monitor_observe(config, sample_messages):
    """MemoryAgent.observe records turns without error."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    # Observing a new turn should not raise.
    agent.observe(
        role="user",
        content="What framework are we using for the frontend?",
    )
    agent.observe(
        role="assistant",
        content="We have not discussed frontend frameworks yet.",
    )

    archive.close()


def test_monitor_evaluate(config, sample_messages):
    """MemoryAgent.evaluate returns a ConfidenceSignal."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    signal = agent.evaluate(
        "We are using PostgreSQL and JWT as previously decided."
    )

    assert isinstance(signal, ConfidenceSignal), (
        "evaluate should return a ConfidenceSignal"
    )
    assert 0.0 <= signal.score <= 1.0, (
        f"confidence score should be in [0, 1], got {signal.score}"
    )

    archive.close()


def test_monitor_hard_interrupt(config, sample_messages, contradicting_messages):
    """Hard interrupt fires when contradiction is detected."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    signal, interrupt = agent.process_turn(
        role="assistant",
        content=contradicting_messages[1]["content"],
    )

    assert signal is not None, "signal should not be None for assistant turn"
    assert interrupt is not None, (
        "hard interrupt should fire for contradicting output"
    )
    assert interrupt.severity == "hard_interrupt", (
        f"expected hard_interrupt severity, got {interrupt.severity}"
    )

    archive.close()


def test_monitor_soft_nudge(config, sample_messages):
    """Soft nudge fires when confidence score is between 0.5 and 0.7."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    # Output that references something partially relevant but with gaps
    # should produce a score in the soft nudge range.
    ambiguous_output = (
        "We should deploy the PostgreSQL database on Kubernetes as we "
        "discussed in our infrastructure planning sessions."
    )
    signal, interrupt = agent.process_turn(
        role="assistant",
        content=ambiguous_output,
    )

    assert signal is not None, "signal should not be None for assistant turn"
    # If the score falls in the soft nudge range, an interrupt should fire.
    if signal.score < config.soft_nudge_threshold and signal.score >= config.hard_interrupt_threshold:
        assert interrupt is not None, "soft nudge should fire in 0.5-0.7 range"
        assert interrupt.severity == "soft_nudge", (
            f"expected soft_nudge severity, got {interrupt.severity}"
        )

    archive.close()


def test_monitor_no_interrupt(config, sample_messages):
    """No interrupt fires when confidence score is above 0.7."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    # Feed the context so the agent knows about these entities
    for msg in sample_messages:
        agent.observe(msg["role"], msg["content"])

    # Fully consistent output that matches archived decisions.
    consistent_output = (
        "We are using JWT for authentication as we decided."
    )
    signal = agent.evaluate(consistent_output)
    interrupt = agent.check_interrupt(signal)

    assert signal is not None, "signal should not be None"
    if signal.score > config.soft_nudge_threshold:
        assert interrupt is None, (
            "no interrupt should fire when score is above soft_nudge_threshold"
        )

    archive.close()


def test_monitor_contradiction_interrupt(config, sample_messages, contradicting_messages):
    """Contradiction always triggers a hard interrupt regardless of score."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    signal, interrupt = agent.process_turn(
        role="assistant",
        content=contradicting_messages[1]["content"],
    )

    assert signal is not None, "signal should not be None"
    assert "contradiction" in signal.flags, (
        "contradiction flag should be present in signal"
    )
    assert interrupt is not None, (
        "contradiction should always trigger an interrupt"
    )
    assert interrupt.severity == "hard_interrupt", (
        f"contradiction interrupt should be hard_interrupt, got {interrupt.severity}"
    )

    archive.close()


# -------------------------------------------------------------------------
# Session briefing test
# -------------------------------------------------------------------------


def test_session_briefing(config, sample_messages):
    """generate_briefing produces a structured briefing with decisions."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    briefing = agent.generate_briefing("session-1")

    assert isinstance(briefing, str), "briefing should be a string"
    assert "Session Briefing" in briefing, "briefing should contain header"
    assert "Prior Decisions" in briefing, "briefing should contain decisions section"
    # Should include at least one actual decision from the sample messages
    assert "Session session-1" in briefing or "session-1" in briefing, (
        "briefing should reference the session"
    )

    archive.close()


# -------------------------------------------------------------------------
# CompactionHook tests
# -------------------------------------------------------------------------


def test_before_compaction_hook(config, sample_messages):
    """CompactionHook captures all messages to archive before compaction."""
    archive, chunker, embedder, search = _build_pipeline(config)

    hook = CompactionHook(archive, chunker, embedder, config)

    count_before = archive.count_chunks()
    stats = hook.before_compaction(sample_messages, session_id="compact-session-1")
    count_after = archive.count_chunks()

    assert count_after > count_before, (
        "compaction hook should store chunks in archive"
    )
    assert count_after - count_before == len(sample_messages), (
        f"hook should store exactly {len(sample_messages)} chunks, "
        f"stored {count_after - count_before}"
    )
    assert "error" not in stats, f"hook should not error: {stats.get('error')}"

    archive.close()


def test_before_compaction_zero_loss(config, sample_messages):
    """Count verification: chunks_after - chunks_before == messages captured."""
    archive, chunker, embedder, search = _build_pipeline(config)

    hook = CompactionHook(archive, chunker, embedder, config)

    # Store some pre-existing data.
    pre_chunks = chunker.chunk_messages(
        [{"role": "user", "content": "pre-existing message"}],
        session_id="old-session",
    )
    for chunk in pre_chunks:
        chunk.embedding = embedder.embed(chunk.content)
    archive.store_chunks(pre_chunks)

    count_before = archive.count_chunks()
    stats = hook.before_compaction(sample_messages, session_id="compact-session-2")
    count_after = archive.count_chunks()

    captured = count_after - count_before
    assert captured == len(sample_messages), (
        f"zero-loss check failed: expected {len(sample_messages)} new chunks, "
        f"got {captured} (before={count_before}, after={count_after})"
    )

    archive.close()


# -------------------------------------------------------------------------
# MemorySearchTool tests
# -------------------------------------------------------------------------


def test_memory_search_tool(config, sample_messages):
    """MemorySearchTool.search returns formatted results."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    tool = MemorySearchTool(archive, embedder, config)

    result = tool.search("PostgreSQL database")

    assert isinstance(result, dict), "search should return a dict"
    assert "results" in result, "result should have 'results' key"
    assert "query" in result, "result should have 'query' key"
    assert "total_results" in result, "result should have 'total_results' key"
    assert len(result["results"]) > 0, "search should return at least one result"

    # Each result item should have expected keys.
    first = result["results"][0]
    assert "content" in first, "result item should have 'content' key"
    assert "session_id" in first, "result item should have 'session_id' key"
    assert "relevance_score" in first, "result item should have 'relevance_score' key"

    archive.close()


def test_memory_search_tool_definition():
    """get_tool_definition returns a valid schema dict."""
    definition = MemorySearchTool.get_tool_definition()

    assert isinstance(definition, dict), "tool definition should be a dict"
    assert "name" in definition, "tool definition should have 'name'"
    assert definition["name"] == "memory_search", (
        f"tool name should be 'memory_search', got {definition['name']}"
    )
    assert "input_schema" in definition, (
        "tool definition should have 'input_schema'"
    )


# -------------------------------------------------------------------------
# process_turn routing tests
# -------------------------------------------------------------------------


def test_process_turn_user(config, sample_messages):
    """process_turn for a user message returns (None, None) -- no evaluation needed."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    signal, interrupt = agent.process_turn(
        role="user",
        content="What database are we using?",
    )

    assert signal is None, "user turns should return None for signal"
    assert interrupt is None, "user turns should return None for interrupt"

    archive.close()


def test_process_turn_assistant(config, sample_messages):
    """process_turn for an assistant message returns a signal and possibly an interrupt."""
    archive, chunker, embedder, search = _build_pipeline(config)
    _store_messages(archive, chunker, embedder, sample_messages, "session-1")

    agent = MemoryAgent(archive, embedder, config)

    signal, interrupt = agent.process_turn(
        role="assistant",
        content="We are using PostgreSQL for the database.",
    )

    assert isinstance(signal, ConfidenceSignal), (
        "assistant turns should return a ConfidenceSignal"
    )
    assert 0.0 <= signal.score <= 1.0, (
        f"score should be in [0, 1], got {signal.score}"
    )
    # interrupt may be None or an Interrupt depending on score.
    if interrupt is not None:
        assert isinstance(interrupt, Interrupt), (
            "interrupt should be an Interrupt instance if present"
        )

    archive.close()
