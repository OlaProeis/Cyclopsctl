# PRD Example: Shorty — Minimal URL Shortener API

> **This is an example PRD showing what to write and how much detail to include.**
> Use it as a template when writing your own `prd.md`.
>
> **Key principle:** Write your PRD the way you would brief a senior developer who is smart but knows nothing about your project. Leave nothing important up to interpretation. The parser turns this document into tasks, and task quality is directly proportional to PRD quality.
>
> **Before you start writing:** Draft your PRD interactively with Opus. Talk through your goals, ask it to challenge your scope, poke holes in the requirements, and help you think through edge cases. Then clean it up into this format. A PRD written in 20 minutes of conversation with Opus will generate far better tasks than one written in isolation.

---

## Overview

Shorty is a minimal URL shortener REST API. A user submits a long URL and gets back a short code (e.g. `abc123`). Visiting the short URL redirects to the original. This is a focused Phase 1 build: no user accounts, no analytics dashboard, no custom slugs. The goal is a working, tested, deployable API that covers the core loop.

**Why this exists:** A lightweight internal tool for sharing links on a team Slack without relying on third-party services.

**Who uses it:** Developers on the team, via API calls or a future frontend (Phase 2).

> **Tip:** Write a crisp overview. One paragraph. What is it, who uses it, and why does it exist. No fluff.

---

## Tech stack

Be explicit. The parser uses this to name files, pick libraries, and make architectural decisions. If you leave the stack open, you will get inconsistent choices across tasks.

- **Language:** Python 3.11
- **Web framework:** FastAPI
- **Database:** SQLite via SQLAlchemy (async, with `aiosqlite`)
- **Migrations:** Alembic
- **Short code generation:** `secrets.token_urlsafe(6)` — 6-character URL-safe random string
- **Testing:** pytest + pytest-asyncio + httpx (async test client)
- **Config:** pydantic-settings, reads from `.env`
- **Entry point:** `uvicorn app.main:app`
- **Project layout:**
  ```
  shorty/
  ├── app/
  │   ├── main.py          # FastAPI app factory and lifespan
  │   ├── config.py        # Settings via pydantic-settings
  │   ├── database.py      # Async SQLAlchemy engine and session factory
  │   ├── models.py        # SQLAlchemy ORM models
  │   ├── schemas.py       # Pydantic request/response schemas
  │   ├── crud.py          # DB operations (create, get, list)
  │   └── routers/
  │       ├── links.py     # POST /links, GET /links
  │       └── redirect.py  # GET /{code}
  ├── alembic/             # Migrations
  ├── tests/
  │   ├── conftest.py      # Async test client and in-memory DB fixture
  │   ├── test_links.py    # Link CRUD endpoint tests
  │   └── test_redirect.py # Redirect endpoint tests
  ├── .env.example
  ├── pyproject.toml
  └── README.md
  ```

> **Tip:** List exact file paths and a directory tree. The parser will put the right code in the right place instead of inventing its own structure.

---

## Phase 1 scope

**In scope for this PRD:**

- Project scaffold: FastAPI app, SQLite DB, Alembic migrations, config
- `POST /links` — create a short link from a long URL
- `GET /links` — list all links (id, code, original URL, created_at)
- `GET /{code}` — redirect to the original URL (HTTP 302)
- `GET /links/{code}` — fetch link metadata (no redirect)
- `DELETE /links/{code}` — soft-delete a link (sets `deleted_at`; redirect returns 410)
- Input validation: reject non-HTTP(S) URLs, blank input
- Error responses: structured JSON with `detail` field
- Full pytest test suite for all endpoints
- `.env` config for `DATABASE_URL` and `BASE_URL`

**Explicitly out of scope for Phase 1:**

- User accounts, authentication, or API keys
- Custom slugs (user-chosen short codes)
- Click tracking or analytics
- Expiry / TTL on links
- Rate limiting
- Any frontend or UI
- Deployment scripts or Docker

> **Tip:** The out-of-scope list is as important as the in-scope list. It stops agents from gold-plating and keeps tasks focused. Be specific about what to skip.

---

## Data model

**Table: `links`**

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER PRIMARY KEY | auto-increment |
| `code` | VARCHAR(16) UNIQUE NOT NULL | 6-char URL-safe random string |
| `original_url` | TEXT NOT NULL | validated HTTP/HTTPS URL |
| `created_at` | DATETIME NOT NULL | UTC, set on insert |
| `deleted_at` | DATETIME NULL | soft delete; NULL means active |

No other tables in Phase 1.

> **Tip:** Define the schema explicitly. If you leave it vague, each task will make different assumptions and they won't compose cleanly.

---

## API contract

### `POST /links`

Create a short link.

**Request body:**
```json
{ "url": "https://example.com/very/long/path" }
```

**Response 201:**
```json
{
  "code": "abc123",
  "short_url": "http://localhost:8000/abc123",
  "original_url": "https://example.com/very/long/path",
  "created_at": "2026-06-08T17:00:00Z"
}
```

**Validation errors (422):** blank URL, non-HTTP/HTTPS scheme.

---

### `GET /links`

List all active (non-deleted) links, newest first.

**Response 200:**
```json
[
  { "code": "abc123", "original_url": "https://...", "short_url": "http://...", "created_at": "..." }
]
```

---

### `GET /{code}`

Redirect to the original URL.

- Active link: HTTP 302, `Location` header set to `original_url`
- Deleted link: HTTP 410 with `{ "detail": "Link has been deleted" }`
- Unknown code: HTTP 404 with `{ "detail": "Link not found" }`

---

### `GET /links/{code}`

Fetch link metadata without redirecting.

- Response 200: same shape as the create response
- 404 if not found; 410 if deleted

---

### `DELETE /links/{code}`

Soft-delete a link by setting `deleted_at = now()`.

- Response 204 on success
- 404 if not found
- 409 if already deleted

---

## Configuration

Read from `.env` (via pydantic-settings in `app/config.py`):

| Key | Default | Description |
|-----|---------|-------------|
| `DATABASE_URL` | `sqlite+aiosqlite:///./shorty.db` | SQLAlchemy async DB URL |
| `BASE_URL` | `http://localhost:8000` | Prepended to codes when generating `short_url` |

---

## Error handling

All error responses use the same shape:
```json
{ "detail": "Human-readable message" }
```

FastAPI validation errors (422) use its default format. All exceptions should be caught at the router level and returned as structured JSON, never as raw Python tracebacks.

---

## Testing requirements

- All tests use an in-memory SQLite DB (`sqlite+aiosqlite:///:memory:`) via a conftest fixture. No test should touch the file-system DB.
- Use `httpx.AsyncClient` with the FastAPI app mounted as transport (no live server).
- Tests must be runnable with `pytest` from the project root.
- Required test coverage:
  - `POST /links`: happy path, duplicate URL (should succeed and create a new code), invalid URL schemes (`ftp://`, empty string)
  - `GET /links`: empty list, list with multiple entries, deleted links should not appear
  - `GET /{code}`: redirect to correct URL, 404 on unknown, 410 on deleted
  - `GET /links/{code}`: happy path, 404, 410
  - `DELETE /links/{code}`: success, 404, 409 on already-deleted

> **Tip:** Spell out exactly what to test and what the edge cases are. "Write tests for the endpoints" produces weak tests. Specific assertions produce real coverage.

---

## Success criteria

The build is complete when:

1. `pytest` passes with no errors from a clean checkout (after `pip install -e ".[dev]"`)
2. Running `uvicorn app.main:app` starts without errors against a fresh DB
3. `POST /links` with a valid URL returns 201 with a `code` and `short_url`
4. `GET /{code}` returns a 302 redirect to the correct URL
5. `DELETE /links/{code}` followed by `GET /{code}` returns 410
6. `GET /links` does not include deleted links

---

## What Phase 2 would cover

*Not for this build, just to show how phases work.*

Phase 2 PRD would add: API key authentication, per-key link ownership, click tracking (impressions table, `GET /links/{code}/stats`), custom slugs, and link expiry. Bug fixes and any rough edges discovered during Phase 1 testing would be included at the top of the Phase 2 PRD as explicit requirements.
