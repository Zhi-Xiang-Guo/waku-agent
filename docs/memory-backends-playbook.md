# Seeing your memories in each provider's own console

The Arena's **Memory** tab reads every backend through Waku's `FactStore`, so it
shows you the same six methods for all of them. That is the point of the
contract — and it is also its blind spot. Each provider stores something
*different* underneath, and the only way to see what they actually did with your
sentences is to open their own console.

This is the walkthrough for that: where the data lands, what you will see there
that Waku's view flattens away, and how to clean up after a benchmark run.

| backend | console | what Waku's view hides |
|---|---|---|
| MemOS | `GET /v1/memories` or the MemOS PostgreSQL authority | extraction decisions, version history, transitions and rebuildable projections |
| Mem0 | [app.mem0.ai](https://app.mem0.ai) → Memories | its extraction decisions, per-memory history |
| Zep | [app.getzep.com](https://app.getzep.com) → your project → Graph | the entity/edge graph and validity intervals |
| LangMem | **none — it is a library, not a service** | nothing; there is no server to look at |
| Supabase | your project → Table editor → the vector table | raw rows and embeddings |
| sqlite | Waku's own **Memory** page, or `.waku/state.db` | nothing — this one is fully visible already |

---

## Before anything: which account and which user are you looking at?

Every hosted backend partitions by a user id, and Waku writes under a stable
default so a single-user install does not scatter its memories across a new
partition per run.

| backend | env var | default |
|---|---|---|
| MemOS | JWT `tenant_id`, `user_id`, `agent_id` claims | no implicit scope |
| Mem0 | `MEM0_USER_ID` | `waku` |
| Zep | `ZEP_USER_ID` | `waku` |
| LangMem | *(namespace is fixed in code)* | — |

If a console looks empty, check this first. Filtering by the wrong user id looks
exactly like "nothing was stored", and those two things mean opposite things.

---

## MemOS — scoped API and PostgreSQL authority

Start MemOS separately and keep it running while Waku uses it. Then configure:

```bash
export WAKU_SEMANTIC_STORE=memos
export WAKU_MEMOS_BASE_URL=http://127.0.0.1:8080
export WAKU_MEMOS_TOKEN="$(python3 /path/to/memos/scripts/generate-dev-jwt.py \
  --tenant waku-local --user local-user --agent waku \
  --subject waku --role USER --role PROJECT_MEMORY_WRITER)"
```

The local helper creates a short-lived development token. A deployed Waku should receive a token
from the same identity provider MemOS verifies. The token is the scope: the adapter never sends
tenant, user, agent, or role values in a request body.

`add()` submits a `DIRECT_MEMORY_COMMAND` and waits for source extraction, authoritative state
transition, and projection jobs to settle. `search()` uses MemOS hybrid retrieval and only exposes
the versions selected into its bounded context. `delete()` calls governed self-service erasure,
which hides the memory in the request transaction and lets the fenced worker remove retained
content and projections. Waku's flat fact view does not show the retained version history or
transition log; inspect those through MemOS.

### Failure boundary (2026-09-07)

`search`, `search_with_ids`, and `list` return an empty list only for a valid
empty result (or an empty/disabled local query). Transport and malformed-response
failures raise content-safe `MemosError`. The management tool registry surfaces
that error instead of telling the model that no matching fact exists. Gated
retrieval callers must explicitly choose an unavailable-memory policy; this
adapter does not silently fall back to an unscoped store or fabricate a miss.
Underlying transport/JSON exception chains are suppressed to avoid exposing
response content in normal formatted tracebacks. This is not a guarantee that
all application instrumentation is content-safe.

The management tool preserves backend-owned fact IDs, including UUIDs and opaque
strings, through update/delete. It does not coerce every fact ID to an integer.

Remaining limitations: materialization success does not by itself prove that
extraction accepted a useful fact; update is multi-step and may partially apply;
write transport failures can still be collapsed to `False`; caller retries
create new operation identities; polling socket timeouts are not yet bounded by
the remaining settle deadline. Do not present this adapter as exactly-once,
atomic-update, strict end-to-end deadline, or production-availability evidence.
The regression tests in `evals/deterministic/test_memos_fact_store.py` prove the
read-error and opaque-ID contracts only; live conformance is separate evidence.

Waku defaults `WAKU_MEMOS_MIN_FUSED_SCORE` to `0.02`. With MemOS's current RRF constant this
filters a rank-only hit from one candidate channel while retaining evidence corroborated by two
channels. Set it to `0` when vector-only recall matters more, and measure the resulting miss rate;
the default is an adapter contract choice, not a benchmarked universal threshold.

---

## Mem0 — [app.mem0.ai](https://app.mem0.ai)

1. Sign in with the account that owns the `MEM0_API_KEY` in your `.env`.
2. Open **Memories** and filter by user `waku`.

**What to look for.** Mem0's product is deciding what is worth remembering, so
compare the sentence you said against the row it kept — they are usually not the
same text. That is the feature, not a bug.

**One caveat that changes how you read a race.** Waku's adapter calls `add()`
with `infer=False`, because the `FactStore` contract says a write must always
store. That deliberately turns Mem0's extraction *off*, so an Arena result
measures its **retrieval**, not its extraction. If you want to see extraction
working, add memories through Mem0's own playground rather than through Waku.

---

## Zep — [app.getzep.com](https://app.getzep.com)

1. Sign in, pick the project whose key is in `ZEP_API_KEY`.
2. **Users** → `waku` → **Graph** for the visual, or **Episodes** for the raw
   text you sent.

**What to look for — this is the one worth filming.** Zep does not keep rows, it
builds a temporal knowledge graph: entities, edges between them, and *validity
intervals*. Ask it for the launch time and it does not rank two competing facts;
the older edge is marked superseded at a point in time. That is a genuinely
different design from every row-based store in the list, and the graph view is
where you can see it.

**Two things that will confuse you if nobody says them:**

- **Ingestion is asynchronous.** `graph.add()` returns in about 0.2s with
  `processed=False`. Data you just wrote may not be searchable for a minute or
  more. Waku's adapter polls until Zep reports `processed=True` (capped by
  `ZEP_MAX_WAIT_SECONDS`, default 120) precisely so a race does not score Zep as
  having forgotten something it was still filing.
- **Episodes ≠ what you get back.** Search returns Zep's rendering of an edge or
  node, not the sentence you sent. Answers read differently from other backends
  even when equally correct.

---

## LangMem — there is no console

LangMem is a **library**, not a hosted service, and this trips people up because
it sits in the same list as two products with dashboards.

- **Without** `WAKU_LANGMEM_POSTGRES`, it runs LangGraph's `InMemoryStore` —
  memory inside the Python process. When the dashboard restarts, it is gone.
  There is nothing to open, and nothing to clean up.
- **With** `WAKU_LANGMEM_POSTGRES=postgresql://…`, it uses `PostgresStore`, and
  "viewing your memories" means querying that database directly.

This is also why the Arena's store card shows LangMem as unreadable rather than
as "0 facts" — every read would construct a fresh empty store, and reporting
that as zero would be a false statement about an empty store instead of a true
one about an unreadable one.

---

## Supabase — your project's table editor

Open the project → **Table editor** → the table named by `SUPABASE_TABLE`. Rows
are visible directly, embeddings included. It is the most transparent of the
hosted options: what you see is what the adapter wrote.

---

## After a benchmark run: cleaning up

**Arena contestants run in throwaway homes**, so a race never touches
`.waku/state.db`. But the *hosted* backends have no such isolation — Mem0 and Zep
write to your real account under user `waku`, and those memories persist after
the run.

Two ways to keep benchmark data out of your real account:

1. **Point the arena somewhere else** — set `MEM0_USER_ID=waku-bench` and
   `ZEP_USER_ID=waku-bench` before a race, and your day-to-day memories stay in
   the `waku` partition.
2. **Delete the benchmark user** in each console when you are done.

Do this before you film. A store card showing your real memories will put your
address and your colleagues' names on screen — the Arena reads live data by
design, and it cannot know which of it you would rather not publish.

---

## What to compare, once you are looking at all of them

The interesting question is not "who scored higher". It is **what each one chose
to keep**:

- Give them all the same sentence and compare the stored form. Mem0 rewrites it,
  Zep decomposes it into a graph, sqlite keeps it nearly verbatim.
- Say something that contradicts an earlier fact, then look at what happened to
  the old one. Row stores leave it sitting there, equally retrievable. Zep marks
  it superseded.
- Then ask the question in a different language, or in different words, and see
  which store still finds it.

That last one is where Waku's own FTS5 store is weakest and the vector-backed
services are strongest — worth knowing before you claim anything on camera.
