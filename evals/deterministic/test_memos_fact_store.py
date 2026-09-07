from __future__ import annotations

import io
import json
from urllib.error import URLError
from urllib.parse import urlsplit

from waku.config import Settings
from waku.memory.semantic.base import FactStore
from waku.memory.semantic.memos_store import MemosFactStore, _query_matches

MEMORY_ID = "00000000-0000-0000-0000-000000000040"
VERSION_ID = "00000000-0000-0000-0000-000000000020"
SOURCE_ID = "00000000-0000-0000-0000-000000000010"


class _Response:
    def __init__(self, payload):
        self.body = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self.body.read(size)


class _MemosApi:
    def __init__(self):
        self.requests = []
        self.invalidated = False

    def __call__(self, request, timeout):
        body = json.loads(request.data) if request.data else None
        self.requests.append((request.method, request.full_url, dict(request.headers), body, timeout))
        path = urlsplit(request.full_url).path
        if request.method == "POST" and path == "/v1/source-events":
            return _Response({"sourceEventId": SOURCE_ID, "disposition": "ACCEPTED"})
        if request.method == "GET" and path.endswith("/materialization"):
            return _Response({"status": "SUCCEEDED"})
        if request.method == "POST" and path == "/v1/retrieval":
            if self.invalidated:
                return _Response({"context": {"selectedVersionIds": []}, "memories": []})
            return _Response(
                {
                    "context": {"selectedVersionIds": [VERSION_ID]},
                    "memories": [
                        {
                            "memoryId": MEMORY_ID,
                            "versionId": VERSION_ID,
                            "subjectKind": "USER",
                            "subjectLabel": "jensen",
                            "normalizedContent": "Jensen always wears a leather jacket",
                            "fusedScore": 0.032,
                            "recordedAt": "2026-09-07T00:00:00Z",
                        }
                    ],
                }
            )
        if request.method == "GET" and path == "/v1/memories":
            return _Response(
                {
                    "items": [
                        {
                            "memoryId": MEMORY_ID,
                            "subject": {"kind": "USER", "label": "jensen"},
                        }
                    ],
                    "nextCursor": None,
                }
            )
        if request.method == "GET" and path.endswith("/current"):
            return _Response(
                {
                    "current": [
                        {
                            "versionId": VERSION_ID,
                            "normalizedContent": "Jensen always wears a leather jacket",
                            "recordedAt": "2026-09-07T00:00:00Z",
                        }
                    ]
                }
            )
        if request.method == "GET" and path == f"/v1/memories/{MEMORY_ID}":
            return _Response(
                {
                    "summary": {
                        "memoryId": MEMORY_ID,
                        "subject": {"kind": "USER", "label": "jensen"},
                        "lockVersion": 1,
                    },
                    "versions": [
                        {
                            "versionId": VERSION_ID,
                            "status": "CURRENT",
                            "normalizedContent": "Jensen always wears a leather jacket",
                            "provenance": {"sourceEventId": SOURCE_ID},
                        }
                    ],
                }
            )
        if request.method == "POST" and path == f"/v1/memories/{MEMORY_ID}/invalidations":
            self.invalidated = True
            return _Response({"lockVersion": 2, "disposition": "APPLIED"})
        if request.method == "POST" and path == f"/v1/deletions/memories/{MEMORY_ID}":
            return _Response({"operationId": SOURCE_ID, "state": "PENDING"})
        raise AssertionError(f"unexpected request: {request.method} {request.full_url}")


def _store(monkeypatch, api):
    monkeypatch.setenv("WAKU_MEMOS_BASE_URL", "http://memos.test")
    monkeypatch.setenv("WAKU_MEMOS_TOKEN", "verified-token")
    monkeypatch.setenv("WAKU_MEMOS_POLL_SECONDS", "0.001")
    return MemosFactStore(Settings(), opener=api)


def test_memos_backend_satisfies_waku_contract_and_maps_every_operation(monkeypatch):
    api = _MemosApi()
    store = _store(monkeypatch, api)

    assert isinstance(store, FactStore)
    store.add("Jensen", "Jensen always wears a leather jacket")
    assert store.settle() is True
    assert store.search("jensen jacket") == ["[jensen] Jensen always wears a leather jacket"]
    assert store.search_with_ids("jensen jacket") == [
        {
            "id": MEMORY_ID,
            "subject": "jensen",
            "content": "Jensen always wears a leather jacket",
            "source": "memos",
            "created_at": "2026-09-07T00:00:00Z",
        }
    ]
    assert store.list(50)[0]["id"] == MEMORY_ID
    assert store.update(MEMORY_ID, "Jensen now wears a raincoat") is True
    assert store.delete(MEMORY_ID) is True

    source_writes = [request for request in api.requests if request[1].endswith("/source-events")]
    assert source_writes[0][3]["payload"] == {
        "content": "jensen: Jensen always wears a leather jacket"
    }
    assert source_writes[0][3]["actorType"] == "USER"
    assert source_writes[0][3]["trustLevel"] == "DIRECT_USER"
    assert source_writes[1][3]["actorType"] == "USER"
    assert source_writes[1][3]["trustLevel"] == "DIRECT_USER"
    invalidation = next(
        request for request in api.requests if request[1].endswith("/invalidations")
    )
    assert invalidation[3] == {
        "versionId": VERSION_ID,
        "sourceEventId": SOURCE_ID,
        "reason": "USER_CORRECTION",
    }
    assert invalidation[2]["If-match"] == '"1"'
    assert all(request[2]["Authorization"] == "Bearer verified-token" for request in api.requests)


def test_memos_backend_returns_misses_without_leaking_transport_failures(monkeypatch):
    def unavailable(request, timeout):
        raise URLError("offline")

    store = _store(monkeypatch, unavailable)

    assert store.search("anything") == []
    assert store.search_with_ids("anything") == []
    assert store.list() == []
    assert store.update("not-a-uuid", "changed") is False
    assert store.delete("not-a-uuid") is False


def test_single_channel_hits_need_a_meaningful_query_term_overlap():
    assert _query_matches("elon launch", "launch", "The launch is in June") is True
    assert _query_matches("nothing here matches this at all", "launch", "The launch is in June") is False
