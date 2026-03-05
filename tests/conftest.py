"""Shared fixtures for the dual-agent memory test suite."""

import os
import tempfile

import pytest

from dual_agent_memory.config import Config, MemoryChunk


@pytest.fixture
def tmp_db(tmp_path):
    """Temporary database path."""
    return str(tmp_path / "test_memory.db")


@pytest.fixture
def config(tmp_db):
    """Test config with temporary database."""
    return Config(db_path=tmp_db)


@pytest.fixture
def sample_messages():
    """Sample conversation messages for testing."""
    return [
        {
            "role": "user",
            "content": (
                "We need to build an authentication system. "
                "Should we use JWT or session-based auth?"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "I recommend we use JWT tokens for authentication. "
                "We decided to go with JWT because it's stateless and works "
                "well with our microservices architecture."
            ),
        },
        {
            "role": "user",
            "content": (
                "Good. What about the database? PostgreSQL or MongoDB?"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "Let's use PostgreSQL. We chose PostgreSQL for its strong "
                "ACID compliance and relational data model which fits our "
                "user management needs."
            ),
        },
        {
            "role": "user",
            "content": (
                "Now let's discuss the API framework. FastAPI or Flask?"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "We decided to use FastAPI for the REST API. It has built-in "
                "async support and automatic OpenAPI documentation which will "
                "speed up development."
            ),
        },
        {
            "role": "user",
            "content": "What about caching? Redis or Memcached?",
        },
        {
            "role": "assistant",
            "content": (
                "We should use Redis for caching. Going with Redis because "
                "it supports data structures beyond simple key-value and has "
                "built-in pub/sub for real-time features."
            ),
        },
    ]


@pytest.fixture
def contradicting_messages():
    """Messages that contradict earlier decisions."""
    return [
        {
            "role": "user",
            "content": "Let's reconsider the database choice.",
        },
        {
            "role": "assistant",
            "content": (
                "We decided to use MongoDB instead. Going with MongoDB for "
                "its flexible schema which is better for our rapidly evolving "
                "data model."
            ),
        },
    ]


@pytest.fixture
def sample_chunks(config, sample_messages):
    """Pre-built chunks from sample messages."""
    from dual_agent_memory.chunker import Chunker

    chunker = Chunker(config)
    return chunker.chunk_messages(sample_messages, session_id="test-session-1")
