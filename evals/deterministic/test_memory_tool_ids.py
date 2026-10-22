"""Search results must carry usable IDs all the way through the tool registry."""

from types import SimpleNamespace

import pytest

from waku.db import connect
from waku.memory.semantic.langmem_store import LangMemFactStore
from waku.memory.semantic.store import SqliteFactStore
from waku.tools.memory_admin import make_manage_memory_tool
from waku.tools.registry import ToolRegistry


class OfflineStore:
    """The LangGraph store boundary, with no SDK, network, or embeddings."""

    def __init__(self, key):
        self.values = {key: {"subject": "alex", "content": "Alex likes tea"}}

    def search(self, namespace, **kwargs):
        return [SimpleNamespace(key=k, value=v) for k, v in self.values.items()]

    def get(self, namespace, key):
        return SimpleNamespace(value=self.values[key]) if key in self.values else None

    def put(self, namespace, key, value):
        self.values[key] = value

    def delete(self, namespace, key):
        del self.values[key]


def registry_for(facts):
    registry = ToolRegistry()
    registry.register(make_manage_memory_tool(SimpleNamespace(facts=facts, episodes=None)))
    return registry


@pytest.mark.parametrize("key", ["fact-012345abcdef", "000123"])
def test_backend_string_id_round_trips_through_search_update_and_delete(key):
    facts = LangMemFactStore.__new__(LangMemFactStore)
    facts.store = OfflineStore(key)
    registry = registry_for(facts)
    assert registry.execute("manage_memory", {"action": "search", "query": "Alex"}) == (
        f"#{key} [alex] Alex likes tea"
    )
    assert registry.execute("manage_memory", {
        "action": "update", "id": key, "content": "Alex likes coffee", "subject": "Alex",
    }) == f"Updated fact #{key}."
    assert facts.store.values[key]["content"] == "Alex likes coffee"
    assert registry.execute("manage_memory", {"action": "delete", "id": key}) == (
        f"Deleted fact #{key}."
    )
    assert facts.store.values == {}
    assert registry.execute("manage_memory", {"action": "delete", "id": key}) == (
        f"No fact with id {key}."
    )


@pytest.mark.parametrize("as_text", [False, True])
def test_sqlite_ids_still_round_trip(as_text, tmp_path):
    conn = connect(tmp_path)
    facts = SqliteFactStore(conn)
    facts.add("alex", "Alex likes tea")
    key = facts.search_with_ids("Alex")[0]["id"]
    if as_text:
        key = str(key)
    registry = registry_for(facts)
    try:
        assert registry.execute("manage_memory", {
            "action": "update", "id": key, "content": "Alex likes coffee",
        }) == f"Updated fact #{key}."
        assert facts.search("coffee") == ["[alex] Alex likes coffee"]
        assert registry.execute("manage_memory", {"action": "delete", "id": key}) == (
            f"Deleted fact #{key}."
        )
        assert facts.list() == []
    finally:
        conn.close()
