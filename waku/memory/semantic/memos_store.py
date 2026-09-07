"""MemOS semantic memory behind Waku's six-method ``FactStore`` contract.

MemOS owns extraction, authorization, temporal truth and rebuildable search
projections. Waku supplies a verified bearer token whose tenant/user/agent
claims define the hard scope; request bodies never carry those identifiers.

Configuration::

    WAKU_SEMANTIC_STORE=memos
    WAKU_MEMOS_BASE_URL=http://127.0.0.1:8080
    WAKU_MEMOS_TOKEN=...  # USER; add PROJECT_MEMORY_WRITER for project facts

The adapter uses only the Python standard library. Writes wait for MemOS's
source-level materialization status so ``add()`` returning means the fact is
searchable, which is the contract Waku's arena and management tools rely on.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from waku.config import Settings
from waku.memory.semantic.base import FactId, env_or

_MAX_RESPONSE_BYTES = 1024 * 1024
_QUERY_STOPWORDS = {
    "a",
    "all",
    "an",
    "and",
    "are",
    "at",
    "does",
    "for",
    "here",
    "is",
    "of",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
}


class MemosError(RuntimeError):
    """A content-safe MemOS transport or contract failure."""


class MemosFactStore:
    def __init__(self, settings: Settings, *, opener=urlopen):
        self.base_url = env_or("WAKU_MEMOS_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("WAKU_MEMOS_BASE_URL must use http or https")
        self.token = os.environ.get("WAKU_MEMOS_TOKEN", "").strip()
        if not self.token:
            raise ValueError("WAKU_MEMOS_TOKEN is required when WAKU_SEMANTIC_STORE=memos")
        self.session_id = env_or("WAKU_MEMOS_SESSION_ID", "waku")
        self.timeout = _positive_float("WAKU_MEMOS_TIMEOUT_SECONDS", "15")
        self.settle_timeout = _positive_float("WAKU_MEMOS_SETTLE_SECONDS", "120")
        self.poll_interval = _positive_float("WAKU_MEMOS_POLL_SECONDS", "0.25")
        self.max_tokens = _positive_int("WAKU_MEMOS_CONTEXT_TOKENS", "1200")
        self.min_fused_score = _non_negative_float("WAKU_MEMOS_MIN_FUSED_SCORE", "0.02")
        self.top_k = settings.retrieval_top_k
        self._opener = opener
        self._pending: set[str] = set()

    def add(self, subject: str, content: str, source: str = "user") -> None:
        subject = subject.strip().lower()
        content = content.strip()
        if not subject or not content:
            raise ValueError("subject and content must not be blank")
        source_event_id = self._ingest(subject, content, source)
        self._pending.add(source_event_id)
        if not self._wait_for_source(source_event_id, self.settle_timeout):
            raise MemosError("MemOS materialization did not become searchable before the deadline")

    def search(self, query: str, top_k: int = 4) -> list[str]:
        return [f"[{row['subject']}] {row['content']}" for row in self._search_rows(query, top_k)]

    def search_with_ids(self, query: str, top_k: int = 8) -> list[dict]:
        return self._search_rows(query, top_k)

    def list(self, limit: int = 200) -> list[dict]:
        if limit < 1:
            return []
        summaries: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(summaries) < limit:
            query = {"limit": min(50, limit - len(summaries))}
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", "/v1/memories?" + urlencode(query))
            items = page.get("items")
            if not isinstance(items, list):
                raise MemosError("MemOS memory page is malformed")
            summaries.extend(item for item in items if isinstance(item, dict))
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        rows: list[dict] = []
        for summary in summaries[:limit]:
            memory_id = _uuid_text(summary.get("memoryId"))
            if memory_id is None:
                continue
            current = self._request("GET", f"/v1/memories/{memory_id}/current")
            rows.extend(self._rows_from_current(summary, current))
        return rows[:limit]

    def update(self, fact_id: FactId, content: str, subject: str | None = None) -> bool:
        memory_id = _uuid_text(fact_id)
        content = content.strip()
        if memory_id is None or not content:
            return False
        try:
            inspection = self._request("GET", f"/v1/memories/{memory_id}")
            summary = inspection.get("summary")
            if not isinstance(summary, dict):
                return False
            old_versions = _active_versions(inspection)
            old_active = set(old_versions)
            if not old_active:
                return False
            effective_subject = subject.strip().lower() if subject else _subject(summary)
            if not effective_subject:
                return False
            source_event_id = self._ingest(effective_subject, content, "user")
            self._pending.add(source_event_id)
            if not self._wait_for_source(source_event_id, self.settle_timeout):
                return False
            if not self._source_created_a_version(source_event_id):
                return False
            current = self._request("GET", f"/v1/memories/{memory_id}")
            still_active = old_active.intersection(_active_versions(current))
            lock_version = _lock_version(current)
            for version_id in sorted(still_active):
                result = self._request(
                    "POST",
                    f"/v1/memories/{memory_id}/invalidations",
                    {
                        "versionId": version_id,
                        "sourceEventId": source_event_id,
                        "reason": "USER_CORRECTION",
                    },
                    {
                        "Idempotency-Key": f"waku-update:{uuid.uuid4()}",
                        "If-Match": f'"{lock_version}"',
                    },
                )
                lock_version = _required_non_negative_int(result.get("lockVersion"))
            if not still_active:
                return True
            old_query = " ".join(old_versions[version_id] for version_id in sorted(still_active))
            return self._wait_until_not_selected(still_active, old_query, self.settle_timeout)
        except (MemosError, ValueError):
            return False

    def delete(self, fact_id: FactId) -> bool:
        memory_id = _uuid_text(fact_id)
        if memory_id is None:
            return False
        try:
            self._request("GET", f"/v1/memories/{memory_id}")
            self._request(
                "POST",
                f"/v1/deletions/memories/{memory_id}",
                {"policyBasis": "USER_REQUEST"},
                {"Idempotency-Key": f"waku-delete:{uuid.uuid4()}"},
            )
            return True
        except (MemosError, ValueError):
            return False

    def settle(self, timeout: float = 120.0) -> bool:
        if timeout <= 0:
            return not self._pending
        deadline = time.monotonic() + timeout
        for source_event_id in list(self._pending):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wait_for_source(source_event_id, remaining):
                return False
        return True

    def _ingest(self, subject: str, content: str, source: str) -> str:
        source_id = f"waku:{uuid.uuid4()}"
        direct_user = source == "user"
        receipt = self._request(
            "POST",
            "/v1/source-events",
            {
                "sourceId": source_id,
                "sessionId": self.session_id,
                "actorType": "USER" if direct_user else "APPLICATION",
                "sourceType": "DIRECT_MEMORY_COMMAND",
                "trustLevel": "DIRECT_USER" if direct_user else "TRUSTED_APPLICATION",
                "occurredAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "payload": {"content": f"{subject}: {content}"},
            },
            {"Idempotency-Key": source_id},
        )
        source_event_id = _uuid_text(receipt.get("sourceEventId"))
        if source_event_id is None or receipt.get("disposition") not in {"ACCEPTED", "DUPLICATE"}:
            raise MemosError("MemOS source receipt is malformed")
        return source_event_id

    def _wait_for_source(self, source_event_id: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._request("GET", f"/v1/source-events/{source_event_id}/materialization")
            state = status.get("status")
            if state == "SUCCEEDED":
                self._pending.discard(source_event_id)
                return True
            if state == "FAILED":
                self._pending.discard(source_event_id)
                return False
            if state != "PROCESSING":
                raise MemosError("MemOS materialization status is malformed")
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        return False

    def _search_rows(self, query: str, top_k: int) -> list[dict]:
        query = query.strip()
        if not query or top_k < 1:
            return []
        response = self._retrieval(query, top_k)
        context = response.get("context")
        memories = response.get("memories")
        if not isinstance(context, dict) or not isinstance(memories, list):
            raise MemosError("MemOS retrieval response is malformed")
        selected = context.get("selectedVersionIds")
        if not isinstance(selected, list):
            raise MemosError("MemOS selected context is malformed")
        selected_ids = {value for value in selected if isinstance(value, str)}
        rows = []
        for memory in memories:
            if not isinstance(memory, dict) or memory.get("versionId") not in selected_ids:
                continue
            memory_id = _uuid_text(memory.get("memoryId"))
            content = memory.get("normalizedContent")
            score = memory.get("fusedScore")
            if (
                memory_id
                and isinstance(content, str)
                and content
                and isinstance(score, int | float)
                and not isinstance(score, bool)
                and (
                    score >= self.min_fused_score
                    or _query_matches(query, _subject(memory), content)
                )
            ):
                rows.append(
                    {
                        "id": memory_id,
                        "subject": _subject(memory),
                        "content": _display_content(content),
                        "source": "memos",
                        "created_at": memory.get("recordedAt"),
                    }
                )
        return rows[:top_k]

    def _wait_until_not_selected(self, version_ids: set[str], query: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self._retrieval(query, max(self.top_k, len(version_ids)))
            context = response.get("context")
            if not isinstance(context, dict):
                raise MemosError("MemOS retrieval response is malformed")
            selected = context.get("selectedVersionIds")
            if not isinstance(selected, list):
                raise MemosError("MemOS selected context is malformed")
            if version_ids.isdisjoint(value for value in selected if isinstance(value, str)):
                return True
            time.sleep(min(self.poll_interval, max(0.0, deadline - time.monotonic())))
        return False

    def _retrieval(self, query: str, top_k: int) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/retrieval",
            {
                "query": query,
                "mode": "HYBRID",
                "limit": min(top_k, 50),
                "componentLimit": max(40, min(top_k, 50)),
                "rerank": False,
                "maxTokens": self.max_tokens,
            },
        )

    def _source_created_a_version(self, source_event_id: str) -> bool:
        cursor: str | None = None
        while True:
            query: dict[str, str | int] = {"limit": 50}
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", "/v1/memories?" + urlencode(query))
            items = page.get("items")
            if not isinstance(items, list):
                raise MemosError("MemOS memory page is malformed")
            for summary in items:
                if not isinstance(summary, dict):
                    continue
                memory_id = _uuid_text(summary.get("memoryId"))
                if memory_id is None:
                    continue
                inspection = self._request("GET", f"/v1/memories/{memory_id}")
                versions = inspection.get("versions")
                if not isinstance(versions, list):
                    raise MemosError("MemOS memory inspection is malformed")
                for version in versions:
                    if not isinstance(version, dict):
                        continue
                    provenance = version.get("provenance")
                    if (
                        isinstance(provenance, dict)
                        and provenance.get("sourceEventId") == source_event_id
                    ):
                        return True
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return False

    @staticmethod
    def _rows_from_current(summary: dict[str, Any], current: dict[str, Any]) -> list[dict]:
        versions = current.get("current")
        if not isinstance(versions, list):
            raise MemosError("MemOS current memory response is malformed")
        memory_id = _uuid_text(summary.get("memoryId"))
        if memory_id is None:
            return []
        rows = []
        for version in versions:
            if not isinstance(version, dict):
                continue
            content = version.get("normalizedContent")
            if isinstance(content, str) and content:
                rows.append(
                    {
                        "id": memory_id,
                        "subject": _subject(summary),
                        "content": _display_content(content),
                        "source": "memos",
                        "created_at": version.get("recordedAt"),
                    }
                )
        return rows

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        request = Request(self.base_url + path, data=data, headers=request_headers, method=method)
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
        except (HTTPError, URLError, TimeoutError, OSError):
            raise MemosError("MemOS request failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise MemosError("MemOS response exceeded the size limit")
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MemosError("MemOS response is not valid JSON") from None
        if not isinstance(value, dict):
            raise MemosError("MemOS response must be an object")
        return value


def _subject(value: dict[str, Any]) -> str:
    raw = value.get("subject")
    if isinstance(raw, dict):
        label = raw.get("label")
        kind = raw.get("kind")
    else:
        label = value.get("subjectLabel")
        kind = value.get("subjectKind")
    if isinstance(label, str) and label.strip():
        return label.strip().lower()
    if isinstance(kind, str) and kind.strip():
        return kind.strip().lower()
    return "memory"


def _uuid_text(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None


def _active_versions(inspection: dict[str, Any]) -> dict[str, str]:
    versions = inspection.get("versions")
    if not isinstance(versions, list):
        raise MemosError("MemOS memory inspection is malformed")
    active = {}
    for version in versions:
        if not isinstance(version, dict) or version.get("status") not in {"CURRENT", "CONFLICTED"}:
            continue
        version_id = _uuid_text(version.get("versionId"))
        content = version.get("normalizedContent")
        if version_id is not None and isinstance(content, str) and content:
            active[version_id] = content
    return active


def _lock_version(inspection: dict[str, Any]) -> int:
    summary = inspection.get("summary")
    if not isinstance(summary, dict):
        raise MemosError("MemOS memory inspection is malformed")
    return _required_non_negative_int(summary.get("lockVersion"))


def _required_non_negative_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise MemosError("MemOS lock version is malformed")
    return value


def _positive_float(name: str, default: str) -> float:
    try:
        value = float(env_or(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def _non_negative_float(name: str, default: str) -> float:
    try:
        value = float(env_or(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative number")
    return value


def _display_content(content: str) -> str:
    return content[:1].upper() + content[1:]


def _query_matches(query: str, subject: str, content: str) -> bool:
    query_terms = {
        term
        for term in re.findall(r"\w+", query.casefold())
        if len(term) >= 3 and term not in _QUERY_STOPWORDS
    }
    evidence_terms = set(re.findall(r"\w+", f"{subject} {content}".casefold()))
    return bool(query_terms.intersection(evidence_terms))


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(env_or(name, default))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value
