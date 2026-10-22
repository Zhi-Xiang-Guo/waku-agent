"""A model's unusable search query must not break retrieval or browse unrelated events."""

import json

import pytest

from evals.helpers import ScriptedClient, response, text_block
from waku.config import Settings
from waku.db import connect
from waku.memory import Memory


@pytest.mark.parametrize("query", [None, "", " \t\n", 42, [], {}])
def test_unusable_query_retrieves_with_the_original_message(tmp_path, monkeypatch, query):
    monkeypatch.setenv("WAKU_SLOT_GATE", "off")
    conn = connect(tmp_path)
    client = ScriptedClient([response([text_block(json.dumps({
        "retrieve": True, "query": query, "reason": "personal",
    }))])])
    memory = Memory(conn, Settings(home=tmp_path), client)
    memory.facts.add("alex", "Alex prefers morning meetings")
    memory.episodes.add("Booked the Acme demo", "2026-10-01")
    try:
        assert memory.gated_retrieve("Alex") == "[alex] Alex prefers morning meetings"
    finally:
        conn.close()
