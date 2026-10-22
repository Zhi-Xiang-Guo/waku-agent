"""An unsearchable request must not turn into a list of unrelated memories."""

import pytest

from waku.db import connect
from waku.memory.episodic.store import SqliteEpisodeStore
from waku.memory.semantic.store import SqliteFactStore


@pytest.fixture
def stores(tmp_path):
    conn = connect(tmp_path)
    facts = SqliteFactStore(conn)
    episodes = SqliteEpisodeStore(conn)
    facts.add("alex", "Alex prefers morning meetings")
    episodes.add("Planned the Acme demo with Alex", "2026-10-01")
    yield facts, episodes
    conn.close()


@pytest.mark.parametrize("query", ["!!!", "___", "a b c", "🔎"])
def test_unsearchable_text_returns_no_matches(stores, query):
    facts, episodes = stores
    assert facts.search(query) == []
    assert facts.search_with_ids(query) == []
    assert episodes.search(query) == []


@pytest.mark.parametrize("query", ["", " \t\n"])
def test_blank_queries_still_browse_recent_memories(stores, query):
    facts, episodes = stores
    assert facts.search_with_ids(query) == facts.list()
    assert episodes.search(query) == episodes.recent()


def test_searchable_text_still_returns_matching_memories(stores):
    facts, episodes = stores
    assert facts.search_with_ids("Alex")[0]["content"] == "Alex prefers morning meetings"
    assert episodes.search("Alex") == ["(2026-10-01) Planned the Acme demo with Alex"]
