# docint workbench analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give docint's chat agent a `run_python` tool that hands a turn's evidence to workbench's MCP door, records each hand-over as a `disclose`, and keeps the tables and charts it returns as an `analysis` artifact that the chat shows and the Report Builder exports. This ships as docint v2.16.0.

**Architecture:** A thin `httpx` client (`docint/utils/workbench_client.py`) speaks revision `2026-07-28` to workbench's `POST /mcp`, sending keyed pseudonyms for the user and the session. A new agent tool (`docint/agents/turn/workbench_tool.py`) builds the inline files from the turn's ledger and facts on the loop thread. Its `fetch` runs the call under a process-wide slot and the turn's deadline. The model reads its reply, and its tables and charts become analyses. A stdlib ledger (`docint/utils/audit_disclose.py`) carries each hand-over to the chat route, which writes one `disclose` child per call. The session store keeps analyses beside their turn (`turn_analyses`). The report renderers gain an `analysis` section.

**Tech Stack:** Python 3.11, uv, FastAPI, httpx (`AsyncClient`, with `MockTransport` in tests), anyio, SQLAlchemy, loguru, markdown-it-py, WeasyPrint; React 19 + Vite + Tailwind v4, `@infra/ui`, vitest.

**Spec:** `docs/superpowers/specs/2026-11-15-v2-mcp-docint-design.md` in this repository (workbench). §4 to §8 bind this plan. §3 is workbench's side of the contract: the request, the result shape, the reason tokens, the headers and the caps.

**Where the work happens:** every path below is relative to the **docint** repository root, and every command runs there. `../deploy` is the sibling deploy checkout. workbench v0.2.0 is built in parallel. The client tests mock its door with `httpx.MockTransport` against §3, so nothing here waits for it.

## Global Constraints

- **Owner decisions (spec §1):** docint gets only `run_python`. Data goes inline with each call, and docint records the hand-over as a `disclose` with purpose. Results come back as the `analysis` artifact (table, charts, code and provenance), exportable to md, html, pdf, json and zip. docint's user id at workbench is a keyed pseudonym of the owner name. Documentation stays proportionate.
- **Earlier decisions that still bind:**
  - Data tools stay in docint.
  - The audit trail records by reference only, never content.
  - Fail closed.
  - Airgap-first.
  - No GPL or AGPL dependency. This plan adds none: `httpx` and `anyio` are already installed, and `anyio` is used directly in `docint/core/api.py` today.
  - Synthetic test data only.
- **Protocol:**
  - The client speaks `2026-07-28` only, to `POST {WORKBENCH_API_BASE}/mcp`, one JSON-RPC message per POST.
  - Every request sends `Authorization: Bearer <WORKBENCH_API_KEY>`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version: 2026-07-28` and `Mcp-Method`.
  - Every body carries `params._meta` with `protocolVersion`, `clientInfo` (`docint` and `docint.__version__`) and `clientCapabilities: {}`.
  - `Mcp-Name: run_python` goes on `tools/call` only, and so do `X-User-Id`, `X-Session-Id`, `X-Audit-Purpose` and `X-Request-ID`.
  - No session id is minted or sent. Identity never travels in `_meta`.
- **Pseudonyms:**
  - `X-User-Id` is HMAC-SHA256 keyed with `WORKBENCH_API_KEY` over `workbench-user/v1`, a NUL byte and `principal.effective_owner`, as 64 hex digits.
  - `X-Session-Id` is the same HMAC over `workbench-session/v1`, NUL and the session id.
  - No edge user name and no client-chosen session id leaves docint.
- **Configuration** (`docint/utils/env_cfg.py` only):
  - `WORKBENCH_API_KEY`: empty means the tool is off. A set key has at least 32 characters, else docint does not start. Its tags are `[secret] [shared: workbench DOCINT_WORKBENCH_KEY]`.
  - `WORKBENCH_API_BASE`: compose `http://workbench-api:8000`.
  - `WORKBENCH_TIMEOUT`: 75.
  - The backend joins `workbench-net` (alias `docint-backend`); the frontend does not. The Makefile's `NETWORKS` gains `workbench-net`.
- **Discovery:**
  - `server/discover`, then `tools/list`, within 5 s together. It is never paid from a call's `t`.
  - It succeeds only when workbench serves `2026-07-28` and lists `run_python` with `inputSchema.properties.time_limit_s.maximum`, the cap.
  - Each result is cached for its own `ttlMs`, and discovery is single-flight.
  - A failure offers no tool on that turn, and is retried at most once per 60 s.
  - An unreachable call, or an `invalid_arguments` refusal, drops the cache.
  - One warning per change of state.
- **Time:**
  - The tool's deadline is `turn_started_at + AGENT_TIME_BUDGET_S` (default 120).
  - When `fetch` starts, it computes `t = min(WORKBENCH_TIMEOUT, time left − 15 s)`. With `t < 20` it sends nothing (`no_time`).
  - The call sends `time_limit_s = min(t − 12, cap)`.
  - `anyio.run` over `httpx.AsyncClient`, inside `anyio.fail_after(t)`. Past it the call answers `unavailable`.
  - Phase timeouts: connect 3 s, write 10 s, pool 3 s.
- **Answers:**
  - The answer is read as a stream. It is refused past 4 MiB, or when it is not `application/json` (SSE is refused). An answer that is not one JSON-RPC message is `unavailable`.
  - Each image must be base64, start with the PNG signature, and be at most 512 KiB.
  - An `isError` result is mapped by `structuredContent.reason`, never by its text:
    - `too_large` → `too_much_data`
    - `queue_full`, `wait_timeout`, `executor_busy` → `no_capacity`
    - `not_completed` → `code_failed`
    - any other token, and any JSON-RPC error → `unavailable`, logged by its token or code.
- **Tool `run_python`:**
  - Arguments: `code` (required), `passages` (integers; left out, every passage shown so far), `title` (at most 80 characters).
  - Limits: one call per round (`call_limit`, "call it again next round"); two accepted calls per turn (`call_limit`, "no more runs in this turn"). A number the evidence does not hold is `no_such_passage`. A refused call leaves the turn's two intact.
  - Files and their caps:
    - `passages.json`, 384 KiB: a list of `{n, text, file, page, time, author, network, kind}`, the text cut to 4 000 characters, every key read only from the visible metadata of `ctx.llm_view(node)`.
    - `keyword_checks.json`, 32 KiB.
    - `topics.json`, 64 KiB.
    - A size is the UTF-8 bytes of `json.dumps(..., ensure_ascii=False)` with no indent. Each file fills in order while the next entry fits.
  - At most 2 calls per process wait on workbench (a non-blocking semaphore, else `no_capacity`).
  - The tool defines no `recall_key`.
  - It is offered after `topics` and before `read`, on `documents` and `all`. It is never offered on `visual` or a scoped turn, and only on a stored turn (`persist and session_id is not None`) while workbench is configured and discovered. When room is short it is the first tool given up, before `topics` and `keyword`.
  - New `ErrorKind` tokens: `call_limit`, `code_failed`, `unavailable`, `too_much_data`. `no_capacity` and `no_time` are reused.
- **Analyses:**
  - Only a run with a table or a chart becomes one; 2 per turn.
  - At most 4 charts, each at most 512 KiB, 1 MiB together.
  - At most 4 tables of at most 500 rows and 32 columns, cells cut to 500 characters, `row_count` kept.
  - The reply ends with, verbatim: "The user sees these tables and charts below your answer as Analysis k; refer to it by that name and cite the passages [n] the code used."
- **Validation:** `ANALYSIS_CHECKS_CHARS` is 1 600. Each analysis contributes its title, the passages it used, the first 600 characters of `stdout`, and its tables' shapes.
- **Audit:**
  - `/query` and `/stream_query` declare `also=("ai_call", "disclose")` while workbench is configured, read at import. Both bind `forwarding_of(op.context)` and `disclosing(op)`. With no stated purpose the forwarded reason is `docint chat`.
  - One `disclose` / `chat.workbench` child per call:
    - target `kind: chunk` with the point ids handed over;
    - `recipient: workbench:run_python`;
    - criteria `workbench_job`, `passages`, `keyword_checks`, `topic_listings`, `data_bytes`;
    - `outcome` `ok`, or `error` when no answer came;
    - at the seal, an in-flight call is `error` with `criteria.incomplete` and no `workbench_job`.
  - `GET /sessions/{session_id}/analyses/{analysis_id}` is `query` / `session.analysis`, target `kind: analysis`. Adding to a report stays `modify` / `report.item_add`.
  - A call is no `ai_call`: it calls no model.
  - Never recorded or logged: code, data, `stdout`, outputs.
- **Reports:**
  - `ARTIFACT_ANALYSIS = "analysis"`. `SECTION_ORDER` is summary, topics, chat, analysis, entities, hate speech. The anchor is `sec-analyses`, and items are numbered.
  - html and pdf charge each table's rows × columns to the report's one `_TableBudget` (10 000 cells, 32 columns). A table past the room left falls back to its CSV in `<pre>`.
  - Charts are PNG data URIs only. The PDF still fetches `data:` only.
  - The server builds the snapshot from the stored row; every client field but `analysis_id` is ignored.
- **docint conventions (its `CLAUDE.md`):**
  - Python `>=3.11,<3.12`; every `os.getenv` lives in `docint/utils/env_cfg.py`; Google-style docstrings.
  - Logging: loguru `{}` placeholders with separate args, never f-strings, and never document text, code, data or output in a line.
  - Strings: prompts in `docint/utils/prompts/en/` and `de/`; report strings in `ui_strings.py` `en` and `de`; SPA strings in `frontend/src/i18n/en.ts` and `de.ts`.
  - `AgentAction` changes land in `docint/agents/types.py`, `frontend/src/api/types.ts` and `frontend/src/components/chat/ChatTurn.tsx`.
  - Every new route gets its row in `docs/audit.md`.
  - SPA: icons from `@infra/ui`, never typed characters; every request through `apiFetch`.
  - `audit_event.py` and `audit_runtime.py` are vendored and stay untouched.
  - Tests use invented data only and no developer-machine paths. Existing test files are edited, never overwritten.
- **Client hygiene (design):** `httpx.AsyncClient(trust_env=False)`, so inline data never goes through a proxy taken from the environment.
- **Release:** `pyproject.toml` gets `version = "2.16.0"`, and `uv lock` refreshes `uv.lock`.
- **Verification:**
  - Every task: its targeted `uv run pytest …`, then `uv run pre-commit run --all-files` and `uv run pytest`.
  - Frontend tasks: also `cd frontend && pnpm test && pnpm build`.
  - Tasks touching `.env.example`: also `python3 -B ../deploy/scripts/check_env_examples.py .`. It reports one `T5` on `WORKBENCH_API_KEY` until deploy's env-sync rows (spec §7, D1) merge, and nothing after.

## Review Focus

1. **The model writes passage numbers as it cites them:** `["[3]", 1.0, 3]`, duplicates, any order. Expected: read like `read(n)` reads them (`passage_number`), each number once, in number order. `[true]` or `["x"]` gets `bad_argument`. Test: Task 5 `test_passage_numbers_read_as_the_model_writes_them`.
2. **What pandas writes:**
   - `df.to_csv("out/x.csv")` adds an unnamed index column, so the header starts with an empty name.
   - `encoding="utf-8-sig"` adds a BOM.
   - Rows can be ragged; a frame can be header-only; a file can be empty.
   Expected: the BOM is stripped, and the empty header name is kept as `""`. Short rows are padded and long rows cut to the header width, marked `truncated`. A header-only CSV is a table with 0 rows. An empty file is no table, and the reply names it. Test: Task 5 `test_tables_read_what_pandas_writes`.
3. **German or CJK passages near a cap:** characters can fit under 384 KiB while their bytes do not. Expected: caps count the UTF-8 bytes of the file as written, never characters. Test: Task 5 `test_caps_count_utf8_bytes_not_characters`.
4. **Code or output that contains a Markdown fence:** a code string with a triple backtick, or `stdout` that prints a Markdown block. Expected: the md export's fence is one backtick longer than the longest run inside, so nothing breaks out. Test: Task 11 `test_md_fences_outlast_the_backticks_inside`.
5. **A PNG within 512 KiB but enormous in pixels:** a decompression bomb from code that retrieved documents steered (spec §6). Expected: the tool reads the IHDR. A chart over `MAX_CHART_SIDE` = 4096 px on a side is dropped, and the reply names it ("too large in pixels"), so no browser or WeasyPrint ever decodes it. Test: Task 5 `test_a_chart_too_large_in_pixels_is_dropped`.

## Branch and pull request

Make `feat/workbench-analysis` from docint's `main` at execution time (`git switch main && git pull && git switch -c feat/workbench-analysis`). Every task ends with one commit on it, and one PR follows Task 13. deploy's D1 (env-sync rows, ADR 0006) merges first. The end-to-end checks (spec §8) are the owner's step once this PR and workbench v0.2.0 have both merged.

---

### Task 1: Configuration and network (I1)

**Files:**
- Modify:
  - `docint/utils/env_cfg.py`: add after `load_nextext_env`.
  - `docker/compose.yaml`: the backend's environment and networks, and the top-level networks.
  - `Makefile`: `NETWORKS`.
  - `.env.example`: `WORKBENCH_NET` under Network & proxy, and a `# --- workbench ---` subsection after `# --- Nextext media processing ---`.
- Test: `tests/test_env_cfg_workbench.py` and `tests/test_compose_workbench.py`, both new.

**Interfaces:**
- Produces:
  - `WORKBENCH_KEY_MIN_LENGTH = 32`
  - `@dataclass(frozen=True) class WorkbenchClientConfig` with `api_base: str`, `timeout: float` and `api_key: str | None = field(default=None, repr=False)`. Its property `enabled -> bool` is true when both the key and the base are set.
  - `load_workbench_env(default_api_base: str = "", default_timeout: float = 75.0) -> WorkbenchClientConfig`. It raises `ValueError` naming the variable and never its value.

- [ ] **Step 1: Write the failing tests**

```python
KEY = "k" * 32

def test_workbench_is_off_without_a_key(monkeypatch) -> None:
    for name in ("WORKBENCH_API_KEY", "WORKBENCH_API_BASE", "WORKBENCH_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_workbench_env()
    assert (cfg.api_key, cfg.api_base, cfg.timeout, cfg.enabled) == (None, "", 75.0, False)

def test_a_key_shorter_than_32_characters_stops_startup(monkeypatch) -> None:
    monkeypatch.setenv("WORKBENCH_API_KEY", "k" * 31)
    with pytest.raises(ValueError, match="WORKBENCH_API_KEY") as caught:
        load_workbench_env()
    assert "k" * 31 not in str(caught.value)

def test_a_key_and_a_base_switch_the_tool_on(monkeypatch) -> None:  # base "http://workbench-api:8000/" -> "http://workbench-api:8000"; enabled
def test_a_blank_key_counts_as_unset(monkeypatch) -> None:          # "   " -> api_key None
def test_a_key_without_a_base_offers_nothing(monkeypatch) -> None:  # enabled False
def test_the_key_stays_out_of_the_repr(monkeypatch) -> None:        # KEY not in repr(cfg)

@pytest.mark.parametrize("raw", ["0", "-5", "nan", "inf", "soon"])
def test_the_timeout_must_be_a_positive_finite_number(monkeypatch, raw: str) -> None:  # ValueError matching WORKBENCH_TIMEOUT
```

```python
# tests/test_compose_workbench.py — text checks, as tests/test_frontend_proxy_config.py reads compose
def test_the_backend_joins_workbench_net_and_the_frontend_does_not() -> None:
    compose = (REPO_ROOT / "docker" / "compose.yaml").read_text(encoding="utf-8")
    backend, frontend = _service(compose, "backend"), _service(compose, "frontend")
    assert "WORKBENCH_API_BASE: ${WORKBENCH_API_BASE:-http://workbench-api:8000}" in backend
    assert "      workbench-net:\n        aliases:\n          - docint-backend" in backend
    assert "workbench-net" not in frontend
    assert "  workbench-net:\n    external: true\n    name: ${WORKBENCH_NET:-workbench-net}" in compose

def test_make_network_creates_workbench_net() -> None:
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    assert "NETWORKS := inference-net data-net edge-net workbench-net" in makefile
```

`_service(text, name)` returns the lines from `  <name>:` up to the next two-space service key, or up to a top-level key.

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_env_cfg_workbench.py tests/test_compose_workbench.py -v`: FAIL (`load_workbench_env` undefined; the compose lines are missing).
- [ ] **Step 3: Implement `load_workbench_env`**: a blank variable counts as unset; the base loses a trailing `/`; the timeout is a float, positive and finite.
- [ ] **Step 4: Edit compose, the Makefile and `.env.example`**
  - Compose, backend:
    - `environment` gets `WORKBENCH_API_BASE: ${WORKBENCH_API_BASE:-http://workbench-api:8000}`.
    - `networks` gets `workbench-net` with the alias `docint-backend`.
  - Compose, top level: `workbench-net` with `external: true` and `name: ${WORKBENCH_NET:-workbench-net}`.
  - Makefile: `NETWORKS := inference-net data-net edge-net workbench-net`.
  - `.env.example`:
    - In Network & proxy, the prose names workbench-net (to workbench) beside the other two, and `# WORKBENCH_NET=workbench-net` follows `# EDGE_NET=edge-net`.
    - The new subsection, every line under 80 columns:

```
# --- workbench ---

# The chat agent's run_python tool runs Python on what a turn retrieved, in
# workbench's sandbox. The key is the same value as DOCINT_WORKBENCH_KEY in
# workbench's .env; make env fills it in. Left out, the tool is off; a key
# under 32 characters stops startup. It is read at startup.
# [secret] [shared: workbench DOCINT_WORKBENCH_KEY]
# WORKBENCH_API_KEY=

# Workbench's api on workbench-net, and the seconds one call may take.
# WORKBENCH_API_BASE=http://workbench-api:8000
# WORKBENCH_TIMEOUT=75
```

- [ ] **Step 5: Verify**
  - Run the Step 2 command: PASS.
  - `uv run pre-commit run --all-files && uv run pytest`: green.
  - `python3 -B ../deploy/scripts/check_env_examples.py .`: only `T5 WORKBENCH_API_KEY` until deploy D1 has merged, then clean.
- [ ] **Step 6: Commit** — `git commit -m "feat(config): workbench client settings; the backend joins workbench-net"`

### Task 2: The workbench client (I2)

**Files:**
- Create: `docint/utils/workbench_client.py`
- Modify:
  - `docint/utils/audit_forward.py`: `system_reason`, `CHAT_REASON`.
  - `tests/conftest.py`: a session-scoped autouse fixture `_hermetic_workbench_env`. It deletes the three `WORKBENCH_*` variables and calls `workbench_client.cache_clear()`, in the pattern of `_hermetic_edge_secret_env`.
- Test: `tests/test_workbench_client.py` and `tests/test_audit_forward.py`, both new.

**Interfaces:**
- Consumes: `WorkbenchClientConfig`, `load_workbench_env` (Task 1); `forward_headers`.
- Produces:
  - In `audit_forward.py`:
    - `CHAT_REASON = "docint chat"`
    - `forward_headers(*, system_reason: str = SYSTEM_REASON) -> dict[str, str]`, which behaves as today when called without arguments.
  - Constants in `workbench_client.py`:
    - `PROTOCOL_VERSION = "2026-07-28"`
    - `META_PROTOCOL = "io.modelcontextprotocol/protocolVersion"`, `META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"`, `META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"`, `META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"`. These are the one place the `_meta` key names live.
    - `DISCOVERY_TIMEOUT_S = 5.0`, `DISCOVERY_RETRY_S = 60.0`
    - `MAX_ANSWER_BYTES = 4 * 1024 * 1024`, `MAX_IMAGE_BYTES = 512 * 1024`
    - `PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"`
  - Types:
    - `FailureKind = Literal["too_much_data", "no_capacity", "code_failed", "unavailable"]`
    - `@dataclass(frozen=True) class Discovery: cap_s: int; server_version: str`
    - `@dataclass(frozen=True) class InlineFile: name: str; content: str`
    - `@dataclass(frozen=True) class OutputFile: name: str; mime: str; size: int; returned: bool`
    - `@dataclass(frozen=True) class RunResult` with `job_id: str`, `stdout: str`, `stderr: str`, `truncated: bool`, `files: tuple[OutputFile, ...]`, `images: tuple[tuple[str, bytes], ...]`, `texts: tuple[tuple[str, str, str], ...]` (name, mime, text), `hint: str | None` and `server_version: str`.
    - `@dataclass(frozen=True) class RunFailure: kind: FailureKind; token: str; job_id: str | None = None; answered: bool = False`. `answered` is true when a JSON-RPC message came back.
  - Functions:
    - `user_pseudonym(key: str, owner: str) -> str`
    - `session_pseudonym(key: str, session_id: str) -> str`
  - `class WorkbenchClient`:
    - `__init__(self, cfg: WorkbenchClientConfig, *, transport: httpx.AsyncBaseTransport | None = None, clock: Callable[[], float] = time.monotonic) -> None`
    - `discover(self) -> Discovery | None`
    - `forget(self) -> None`
    - `run_python(self, *, code: str, data: Sequence[InlineFile], time_limit_s: int, owner: str, session_id: str, timeout_s: float) -> RunResult | RunFailure`. It never raises.
    - `_timeout(self, t: float) -> httpx.Timeout`
  - `@functools.cache def workbench_client() -> WorkbenchClient | None`: the process-wide client, `None` while `load_workbench_env().enabled` is false.

- [ ] **Step 1: Write the failing tests**

  In the test file, `FakeDoor` is an `httpx.MockTransport` handler that records every request and answers by `Mcp-Method`. By default:
  - `server/discover` answers `{"supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18"], "capabilities": {"tools": {"listChanged": False}}, "instructions": "…", "ttlMs": 3600000, "cacheScope": "private", "resultType": "complete", "_meta": {META_SERVER_INFO: {"name": "workbench", "version": "0.2.0"}}}`.
  - `tools/list` answers one `run_python` tool whose `inputSchema.properties.time_limit_s` is `{"type": "integer", "minimum": 5, "maximum": 60}`, with `ttlMs` 300000.
  - `tools/call` answers the §3.4 result: one `chart.png` image, one `counts.csv` resource, `job_id` `"0" * 32`.

  `FakeClock` advances by hand. `CFG = WorkbenchClientConfig(api_base="http://workbench.test", timeout=75.0, api_key=KEY)`.

```python
def test_discovery_reads_the_cap_and_the_version() -> None:
    door = FakeDoor()
    assert WorkbenchClient(CFG, transport=httpx.MockTransport(door)).discover() == Discovery(cap_s=60, server_version="0.2.0")
    assert door.methods() == ["server/discover", "tools/list"]

@pytest.mark.parametrize("change", ["no_modern_revision", "no_run_python", "no_maximum", "not_json_rpc"])
def test_discovery_offers_nothing_without_the_modern_revision_and_a_capped_run_python(change: str) -> None:  # -> None

def test_each_result_is_cached_for_its_ttl() -> None:
    client.discover(); clock.advance(299); client.discover()
    assert door.methods() == ["server/discover", "tools/list"]
    clock.advance(2); client.discover()
    assert door.methods() == ["server/discover", "tools/list", "tools/list"]
    clock.advance(3600); client.discover()
    assert door.methods()[-2:] == ["server/discover", "tools/list"]

def test_concurrent_turns_share_one_discovery() -> None:  # 8 threads behind a Barrier, the door slow by 0.2 s: two requests in all, 8 equal results
def test_a_failed_discovery_is_tried_again_at_most_once_a_minute() -> None:  # 503 -> None; +59 s -> None, no request; +2 s -> requests again
def test_discovery_gives_up_after_five_seconds(monkeypatch) -> None:  # DISCOVERY_TIMEOUT_S=0.2, an async handler sleeping 1 s -> None within 0.9 s

def test_every_request_states_the_modern_revision() -> None:  # after discover() and one run_python()
    for request, body in door.requests:
        assert request.headers["authorization"] == f"Bearer {KEY}"
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json, text/event-stream"
        assert request.headers["mcp-protocol-version"] == "2026-07-28"
        assert request.headers["mcp-method"] == body["method"]
        assert body["params"]["_meta"] == {
            META_PROTOCOL: "2026-07-28", META_CLIENT_INFO: {"name": "docint", "version": __version__}, META_CLIENT_CAPS: {}}
    assert [("mcp-name" in r.headers) for r, _ in door.requests] == [False, False, True]
    assert door.requests[-1][0].headers["mcp-name"] == "run_python"

def test_the_user_and_the_session_reach_workbench_as_keyed_pseudonyms() -> None:
    call, _ = door.requests[-1]
    assert call.headers["x-user-id"] == hmac.new(KEY.encode(), b"workbench-user/v1\x00user-a", hashlib.sha256).hexdigest()
    assert call.headers["x-session-id"] == hmac.new(KEY.encode(), b"workbench-session/v1\x00session-1", hashlib.sha256).hexdigest()
    assert "user-a" not in str(call.headers) and "session-1" not in str(call.headers)

def test_a_new_key_gives_new_pseudonyms() -> None:  # user_pseudonym(KEY, "user-a") != user_pseudonym("j" * 32, "user-a"); both 64 hex
def test_the_call_states_the_turns_purpose_else_docint_chat() -> None:  # inside forwarding(case_ref="case-0001", reason="review", request_id="req-0001") -> those two headers; unbound -> reason "docint chat"
def test_the_call_sends_its_code_data_and_time_limit() -> None:  # params == {"name": "run_python", "arguments": {"code": CODE, "data": [{"name": "passages.json", "content": "[]"}], "time_limit_s": 23}, "_meta": ...}
def test_one_deadline_bounds_the_whole_call() -> None:  # async handler sleeping 2 s, timeout_s=0.3 -> RunFailure("unavailable", ..., answered=False) within 1.5 s
def test_the_phase_timeouts_stay_as_guards() -> None:  # client._timeout(40.0) == httpx.Timeout(40.0, connect=3.0, write=10.0, pool=3.0)

@pytest.mark.parametrize(("content_type", "body"), [
    ("text/event-stream", b"data: {}\n\n"), ("application/json", b"x" * (4 * 1024 * 1024 + 1)),
    ("application/json", b"not json"), ("application/json", b'{"detail": "not found"}')])
def test_an_answer_that_is_not_one_small_json_rpc_message_is_unavailable(content_type: str, body: bytes) -> None:

@pytest.mark.parametrize(("token", "kind"), [
    ("too_large", "too_much_data"), ("queue_full", "no_capacity"), ("wait_timeout", "no_capacity"),
    ("executor_busy", "no_capacity"), ("not_completed", "code_failed"), ("refused", "unavailable"),
    ("purpose_required", "unavailable"), ("invalid_arguments", "unavailable"), ("something_new", "unavailable")])
def test_a_refused_run_is_mapped_by_its_token_never_its_text(token: str, kind: str) -> None:
    # every isError answer's text reads "busy, retry later"; structuredContent.reason is the token
    assert result == RunFailure(kind=kind, token=token, job_id=None, answered=True)

def test_a_json_rpc_error_is_unavailable() -> None:  # {"error": {"code": -32603, ...}} -> RunFailure("unavailable", "jsonrpc:-32603", answered=True)
@pytest.mark.parametrize("image", ["not base64!", "<base64 of a GIF header>", "<base64 of PNG_SIGNATURE + 512 KiB>"])
def test_an_image_must_be_a_png_within_512_kib(image: str) -> None:  # -> unavailable, answered True
def test_a_successful_run_returns_its_outputs() -> None:  # job_id, stdout, files, images (("chart.png", b"\x89PNG…"),), texts (("counts.csv", "text/csv", "author,posts\n…"),), server_version "0.2.0"
def test_an_unreachable_call_or_a_refused_argument_drops_the_discovery() -> None:  # ConnectError, then invalid_arguments: the next discover() asks the door again each time
def test_one_warning_per_change_of_state(loguru_caplog) -> None:  # three failed discoveries 61 s apart -> 1 WARNING; success -> 1 INFO; failure -> 1 more WARNING
def test_the_log_holds_no_code_data_or_output(loguru_caplog) -> None:  # code "print('Hafenfest')", content "Hafenfest", stdout "Hafenfest 12": absent from every line, on success and on each failure
def test_the_client_takes_no_proxy_from_the_environment(monkeypatch) -> None:  # HTTPS_PROXY set; the AsyncClient it builds has trust_env False
def test_without_a_key_there_is_no_client(monkeypatch) -> None:  # workbench_client() is None
```

```python
# tests/test_audit_forward.py
def test_an_unstated_purpose_falls_back_to_the_reason_given() -> None:
    assert parse_purpose_header(forward_headers(system_reason=CHAT_REASON)[PURPOSE_HEADER]) == (None, "docint chat")
    assert parse_purpose_header(forward_headers()[PURPOSE_HEADER]) == (None, "docint ingest")
def test_a_stated_purpose_wins_over_any_fallback() -> None:  # inside forwarding(case_ref="case-0001"): forward_headers(system_reason=CHAT_REASON) carries case-0001
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_workbench_client.py tests/test_audit_forward.py -v`: FAIL (import error).
- [ ] **Step 3: Implement the client**
  - Every exchange runs as `anyio.run` over a fresh `httpx.AsyncClient(base_url=…, transport=…, trust_env=False, timeout=self._timeout(t))`.
    - Discovery runs inside `anyio.fail_after(DISCOVERY_TIMEOUT_S)`; a call runs inside `anyio.fail_after(timeout_s)`.
    - The answer is read with `client.stream(...)`, counting bytes up to `MAX_ANSWER_BYTES`.
  - Discovery: one `threading.Lock` makes it single-flight. Each result is kept with its own expiry. `forget()` empties both.
  - Images are paired with the `files` entries that are `returned` and end in `.png`, in order. A count mismatch is `unavailable`.
  - A resource's name is the last segment of its URI, percent-decoded.
  - `RunResult.server_version` comes from the result's `_meta` serverInfo, else from the cached discovery.
  - `forget()` runs on a transport error, on a timeout, and on the token `invalid_arguments`.
  - Each discovery state change logs once, by kind or code.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(workbench): MCP client for run_python, with keyed pseudonyms and one deadline per call"`

### Task 3: Registry hooks — facts, artifacts, rounds (I3, part 1)

**Files:**
- Modify:
  - `docint/agents/turn/tools.py`: `ErrorKind`, `ToolContext`, `HasBeginRound`, `ToolRegistry.begin_round` and `finish`.
  - `docint/agents/turn/calls.py`: `RoundCalls.prepare`.
  - `docint/agents/turn/loop.py`: `AgentResult`, `_result`.
- Test: `tests/test_agent_tools.py`, `tests/test_agent_loop.py`

**Interfaces:**
- Produces:
  - `ErrorKind` gains `"call_limit"`, `"code_failed"`, `"unavailable"` and `"too_much_data"`. Its docstring names them as the run_python tool's.
  - `ToolContext.facts: list[dict[str, Any]]` and `ToolContext.artifacts: list[dict[str, Any]]`, both `field(default_factory=list, init=False, repr=False, compare=False)` like `answered`.
  - `@runtime_checkable class HasBeginRound(Protocol): def begin_round(self) -> None`. It is optional, as `HasRecallKey` is.
  - `ToolRegistry.begin_round(self) -> None` calls `begin_round()` on every offered tool that has it.
  - `ToolRegistry.finish`: after a work's `answer` ran (never for a reply recalled from memory, an error, or a reply without a trail), it appends `{"action": reply.action, "detail": reply.detail, **reply.trail}` to `ctx.facts`.
  - `RoundCalls.prepare` calls `self._registry.begin_round()` before it prepares the round's first call.
  - `AgentResult.analyses: list[dict[str, Any]] = field(default_factory=list)`; `AgentLoop._result` passes `list(turn.ctx.artifacts)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_reply_with_a_trail_is_kept_as_a_fact() -> None:  # a keyword call -> ctx.facts == [{"action": "keyword", "detail": "Hafenfest", "field": "text", "matches": 3}]
def test_a_topics_listing_is_a_fact_though_it_shows_no_passage() -> None:  # facts[0]["action"] == "topics", with "topics" and "total"
def test_a_reply_answered_from_memory_adds_no_fact() -> None:  # the same keyword call twice -> one fact
def test_an_error_reply_adds_no_fact() -> None:
def test_each_round_begins_for_the_tools_that_ask() -> None:  # a fake tool counting begin_round(); RoundCalls.prepare twice -> 2; the search tool beside it needs no begin_round
def test_a_turns_artifacts_reach_its_result() -> None:  # scripted_loop over a fake tool whose answer appends {"title": "t"} to ctx.artifacts -> AgentResult.analyses == [{"title": "t"}]
def test_the_run_python_error_kinds_exist() -> None:  # {"call_limit", "code_failed", "unavailable", "too_much_data"} <= set(get_args(ErrorKind))
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_tools.py tests/test_agent_loop.py -v -k "fact or begin or artifacts or error_kinds"`: FAIL.
- [ ] **Step 3: Implement the hooks as listed.**
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(agent): tool replies with a trail become turn facts; rounds begin; turns carry artifacts"`

### Task 4: The disclosure ledger (I5, part 1; moved ahead because the tool's fetch writes to it)

**Files:**
- Create: `docint/utils/audit_disclose.py` (stdlib only, like `audit_meter.py`)
- Test: `tests/test_audit_disclose.py`

**Interfaces:**
- Produces:
  - `class DisclosureRefused(RuntimeError)`
  - `@dataclass(frozen=True) class DisclosureRecord` with `point_ids: tuple[str, ...]`, `passages: int`, `keyword_checks: int`, `topic_listings: int`, `data_bytes: int`, `job_id: str | None`, `outcome: Literal["ok", "error"]` and `incomplete: bool`.
  - `class Disclosure`, a handle:
    - `answered(self, job_id: str | None) -> None`
    - `unanswered(self) -> None`
    - Both do nothing once the disclosure was written.
  - `class DisclosureLedger`, thread-safe under one `threading.Lock`:
    - `add(self, *, point_ids: Sequence[str], passages: int, keyword_checks: int, topic_listings: int, data_bytes: int) -> Disclosure`. Raises `DisclosureRefused` once sealed.
    - `take(self) -> list[DisclosureRecord]` returns every unwritten disclosure as it stands, in order, and marks it written:
      - answered → `ok`, with its job;
      - unanswered → `error`;
      - in flight → `error`, `incomplete=True`, no job.
    - `seal(self) -> list[DisclosureRecord]` runs `take()` and refuses every later `add`, atomically.
    - `sealed: bool` (a property).
  - `binding(ledger: DisclosureLedger) -> ContextManager[DisclosureLedger]`, over a `ContextVar`, so the ledger follows the work into worker and search threads (both copy the context).
  - `current_ledger() -> DisclosureLedger | None`
  - `record_disclosure(*, point_ids, passages, keyword_checks, topic_listings, data_bytes) -> Disclosure` raises `DisclosureRefused` when no ledger is bound, or the bound one is sealed.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_disclosure_is_taken_as_it_stands() -> None:
    ledger = DisclosureLedger()
    with binding(ledger):
        done = record_disclosure(point_ids=["p1", "p2"], passages=2, keyword_checks=1, topic_listings=0, data_bytes=2048)
        lost = record_disclosure(point_ids=["p3"], passages=1, keyword_checks=0, topic_listings=0, data_bytes=900)
        flying = record_disclosure(point_ids=["p4"], passages=1, keyword_checks=0, topic_listings=0, data_bytes=800)
    done.answered("0" * 32); lost.unanswered()
    assert [(r.outcome, r.job_id, r.incomplete) for r in ledger.take()] == [("ok", "0" * 32, False), ("error", None, False), ("error", None, True)]
    assert ledger.take() == []

def test_nothing_is_recorded_after_the_seal() -> None:  # seal(); record_disclosure(...) raises DisclosureRefused
def test_nothing_is_recorded_without_a_ledger() -> None:  # no binding -> DisclosureRefused
def test_an_answer_after_the_write_changes_nothing() -> None:  # seal() writes it in flight; .answered(job) later -> take() == []
def test_the_ledger_follows_the_work_into_another_thread() -> None:  # contextvars.copy_context().run in a Thread -> the bound ledger holds its disclosure
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_audit_disclose.py -v`: FAIL (import error).
- [ ] **Step 3: Implement `audit_disclose.py`** (stdlib only, no logging).
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(audit): a ledger of what a turn hands to workbench, sealed when the turn ends"`

### Task 5: The `run_python` tool (I3, part 2)

**Files:**
- Create: `docint/agents/turn/workbench_tool.py`
- Modify: `tests/agent_fakes.py`. Add `FakeWorkbench`, which answers from a script and records each call and the ledger's state at that moment, `run_result(**overrides) -> RunResult`, and `tiny_png(width=640, height=480) -> bytes`, which builds the signature plus an IHDR chunk.
- Test: `tests/test_agent_workbench_tool.py` (new)

**Interfaces:**
- Consumes:
  - Task 2: `Discovery`, `InlineFile`, `RunResult`, `RunFailure`, and `WorkbenchClient.run_python`, through the protocol `WorkbenchCaller`.
  - Task 3: `ToolContext.facts`, `ToolContext.artifacts`, `begin_round`.
  - Task 4: `record_disclosure`, `DisclosureRefused`.
  - Existing: `passage_number`, `passage_range`, `error_reply`, `trail_detail`, and `source_file_hash` / `source_file_name` (`docint/core/summary/units.py`, stdlib only).
- Produces:
  - Constants:
    - `NAME = "run_python"`, `MAX_CODE_CHARS = 50_000`, `TITLE_CHARS = 80`, `PASSAGE_TEXT_CHARS = 4_000`
    - `PASSAGES_CAP = 384 * 1024`, `KEYWORD_CHECKS_CAP = 32 * 1024`, `TOPICS_CAP = 64 * 1024`
    - `CALLS_PER_TURN = 2`, `WORKBENCH_SLOTS = 2`, `WORKBENCH_SLOT_POOL = threading.BoundedSemaphore(WORKBENCH_SLOTS)`
    - `TURN_RESERVE_S = 15.0`, `MIN_CALL_S = 20.0`, `START_MARGIN_S = 12.0`
    - `MAX_TABLES = 4`, `MAX_CHARTS = 4`, `MAX_ROWS = 500`, `MAX_COLUMNS = 32`, `CELL_CHARS = 500`
    - `CHART_BYTES = 512 * 1024`, `CHARTS_BYTES = 1024 * 1024`, `MAX_CHART_SIDE = 4096`
    - `ANALYSIS_NOTE = "The user sees these tables and charts below your answer as Analysis {k}; refer to it by that name and cite the passages [n] the code used."`
  - `@dataclass(frozen=True) class PythonTurn: owner: str; session_id: str; deadline: float`. `deadline` is on `time.monotonic()`'s scale.
  - `class WorkbenchCaller(Protocol)`: `run_python(self, *, code, data, time_limit_s, owner, session_id, timeout_s) -> RunResult | RunFailure`
  - `class WorkbenchPythonTool`:
    - `__init__(self, client: WorkbenchCaller, discovery: Discovery, turn: PythonTurn, *, clock: Callable[[], float] = time.monotonic, slots: threading.Semaphore = WORKBENCH_SLOT_POOL) -> None`
    - attributes `name`, `description`, `parameters`
    - `begin_round(self) -> None`
    - `prepare(self, arguments: Mapping[str, Any], ctx: ToolContext) -> ToolReply | ToolWork`
  - `parse_table(name: str, text: str) -> dict[str, Any] | None`
  - The analysis dict appended to `ctx.artifacts`. Later tasks rely on these keys:

```
{"schema": 1, "title": str, "code": str, "stdout": str,
 "tables": [{"name": str, "columns": list[str], "rows": list[list[str]], "row_count": int, "truncated": bool}],
 "charts": [{"name": str, "data_uri": "data:image/png;base64,…", "bytes": int}],
 "provenance": {"sources": [{"citation_index": int, "chunk_id": str, "file_hash": str | None, "filename": str | None, "page": int | None}],
                "inputs": {"passages": int, "keyword_checks": int, "topic_listings": int},
                "workbench": {"job_id": str, "version": str, "tool": "run_python"},
                "created_at": "<ISO-8601 UTC>"}}
```

- [ ] **Step 1: Write the failing tests** (constants as in Task 2)

  Shared fixtures for the tests below:
  - `TURN = PythonTurn("user-a", "session-1", deadline=1_000.0)`
  - `DISCOVERY = Discovery(60, "0.2.0")`
  - `_tool(workbench, now=880.0)` leaves 120 s.
  - `_held(ctx, *passages)` admits invented passages.
  - Fetches run inside `binding(DisclosureLedger())`.

```python
def test_a_parallel_pair_in_one_round_runs_one_and_refuses_the_other() -> None:
    registry = ToolRegistry([tool]); registry.begin_round()
    first, second = two_phase_round(registry, [tool_call("run_python", code=CODE), tool_call("run_python", code=CODE)], ctx)
    assert not first.error
    assert (second.error, second.kind) == (True, "call_limit") and "call it again next round" in second.text
    registry.begin_round()
    [third] = two_phase_round(registry, [tool_call("run_python", code=CODE)], ctx)
    assert not third.error and len(workbench.calls) == 2

def test_refusals_leave_the_turns_two_intact() -> None:
    # round 1 passages=[99] -> no_such_passage; round 2 runs; round 3 a pair: the first runs, the second call_limit;
    # round 4 -> call_limit with "no more runs in this turn"; len(workbench.calls) == 2
def test_a_repeated_call_runs_again() -> None:  # the same arguments in rounds 1 and 2 both reach the workbench double

def test_passages_json_holds_only_what_the_model_may_see() -> None:
    # passage p1: text, filename "harbour-report.pdf", page 4, file_path "srv/hidden/report.pdf" (hidden by llm_view)
    passages = json.loads(_sent(workbench)["passages.json"])
    assert passages == [{"n": 1, "text": TEXT, "file": "harbour-report.pdf", "page": 4,
                         "time": None, "author": None, "network": None, "kind": None}]
    assert "srv/hidden" not in _sent(workbench)["passages.json"]

@pytest.mark.parametrize(("metadata", "key", "value"), [
    ({"origin": {"filename": "minutes.docx"}}, "file", "minutes.docx"), ({"page_number": 7}, "page", 7),
    ({"origin": {"page_number": 9}}, "page", 9), ({"reference_metadata": {"timestamp": "2026-05-01T10:00:00Z"}}, "time", "2026-05-01T10:00:00Z"),
    ({"start_ts": "00:01:05"}, "time", "00:01:05"), ({"reference_metadata": {"author": "user-a"}}, "author", "user-a"),
    ({"speaker": "SPEAKER_01"}, "author", "SPEAKER_01"), ({"reference_metadata": {"network": "telegram"}}, "network", "telegram"),
    ({"reference_metadata": {"type": "posting"}}, "kind", "posting"), ({"docint_doc_kind": "transcript"}, "kind", "transcript")])
def test_each_field_falls_back_as_the_spec_lists(metadata: dict[str, Any], key: str, value: Any) -> None:

def test_a_passages_text_is_cut_to_4000_characters() -> None:
def test_left_out_passages_means_every_passage_shown() -> None:  # three held, no passages argument -> n == [1, 2, 3]
def test_passage_numbers_read_as_the_model_writes_them() -> None:  # Review Focus 1: ["[3]", 1.0, 3] -> [1, 3]; [True] and ["x"] -> bad_argument
def test_a_number_the_evidence_does_not_hold_is_no_such_passage() -> None:  # passages=[9] with three held -> "there is no passage [9]; the passages run from [1] to [3]."
def test_code_must_be_given_and_fit() -> None:  # no code -> missing_argument; 50_001 characters -> too_long; nothing sent either way

def test_keyword_checks_and_topics_come_from_the_turns_facts() -> None:
    ctx.facts.extend([KEYWORD_FACT, TOPICS_FACT, {"action": "search", "detail": "x"}])
    sent = _sent(workbench)
    assert list(sent) == ["passages.json", "keyword_checks.json", "topics.json"]
    assert json.loads(sent["keyword_checks.json"]) == [KEYWORD_FACT] and json.loads(sent["topics.json"]) == [TOPICS_FACT]
    # with no facts: list(sent) == ["passages.json"]

def test_each_file_fills_in_order_while_the_next_entry_fits(monkeypatch) -> None:  # PASSAGES_CAP shrunk: [1] and [2] go, [3] stays; the reply names [3] as left out
def test_caps_count_utf8_bytes_not_characters(monkeypatch) -> None:  # Review Focus 3: "Größe Übersicht " text under the cap in characters, over it in bytes -> left out
def test_the_provenance_stays_in_docint() -> None:  # artifacts[0]["provenance"]["sources"] == [{"citation_index": 1, "chunk_id": "p1", "file_hash": HASH, "filename": "harbour-report.pdf", "page": 4}]; "p1" and HASH absent from what was sent

def test_a_run_needs_twenty_seconds_and_sends_nothing_without_them() -> None:  # now = deadline - 34.9 -> no_time; workbench.calls == []; ledger.take() == []
@pytest.mark.parametrize(("left", "time_limit", "timeout"), [(200.0, 60, 75.0), (90.0, 60, 75.0), (50.0, 23, 35.0), (35.0, 8, 20.0)])
def test_the_time_limit_leaves_room_for_start_up_and_the_turn(left: float, time_limit: int, timeout: float) -> None:
def test_two_calls_at_once_are_the_most_a_process_sends() -> None:  # slots=BoundedSemaphore(2), both held -> no_capacity, nothing sent, nothing disclosed

def test_the_disclosure_is_recorded_before_the_send_and_named_after() -> None:
    assert workbench.ledger_states == [1]          # one disclosure in flight when the call went out
    [record] = ledger.take()
    assert record == DisclosureRecord(point_ids=("p1", "p2"), passages=2, keyword_checks=0, topic_listings=0,
                                      data_bytes=DATA_BYTES, job_id="0" * 32, outcome="ok", incomplete=False)

def test_a_sealed_ledger_sends_nothing() -> None:  # ledger.seal() before the fetch -> unavailable; workbench.calls == []
@pytest.mark.parametrize("kind", ["no_capacity", "too_much_data", "code_failed", "unavailable"])
def test_a_refused_run_is_an_error_reply_of_its_kind(kind: str) -> None:  # reply.kind == kind; ctx.artifacts == []; disclosed answered or unanswered per RunFailure.answered

def test_the_reply_fits_and_names_the_analysis() -> None:
    assert len(reply.text) <= ctx.budget.result_chars
    assert reply.text.endswith(ANALYSIS_NOTE.format(k=1))
    assert "counts.csv: 2 rows × 2 columns" in reply.text and "chart.png" in reply.text
    assert (reply.found, reply.informative, reply.detail) == (0, True, "Posts per author")
    assert reply.trail == {"analysis": 1, "passages": 2, "tables": 1, "charts": 1}

def test_a_long_stdout_is_cut_in_the_middle() -> None:  # "HEAD" + "x" * 50_000 + "TAIL": both ends and a cut marker, within result_chars
def test_only_a_run_with_a_table_or_a_chart_becomes_an_analysis() -> None:  # only notes.txt -> artifacts == []; the reply names "notes.txt (120 bytes)"; trail["analysis"] is None
def test_tables_read_what_pandas_writes() -> None:  # Review Focus 2
    assert parse_table("x.csv", "﻿,author,posts\n0,user-a,12\n1,user-b\n") == {
        "name": "x.csv", "columns": ["", "author", "posts"], "rows": [["0", "user-a", "12"], ["1", "user-b", ""]],
        "row_count": 2, "truncated": False}
    # a row longer than the header -> cut, truncated True; header only -> rows [], row_count 0; "" -> None
def test_tables_and_charts_are_bounded() -> None:  # 5 tables -> 4; 600 rows -> 500, row_count 600, truncated; 40 columns -> 32; a 600-character cell -> 500; 5 charts -> 4; three charts of 400 KiB -> the first two
def test_a_chart_too_large_in_pixels_is_dropped() -> None:  # Review Focus 5: tiny_png(20_000, 20_000) -> not stored; the reply says "too large in pixels"
def test_the_analysis_carries_its_code_output_and_provenance() -> None:  # schema 1, title, code, stdout, inputs, workbench {"job_id": "0" * 32, "version": "0.2.0", "tool": "run_python"}, created_at in UTC
def test_the_description_names_no_other_tool_and_stays_short() -> None:  # no \b(search|read|keyword|topics|search_images|keyframes)\b; at most 60 words
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_workbench_tool.py -v`: FAIL (import error).
- [ ] **Step 3: Implement `WorkbenchPythonTool`**
  - The description and schema:
    - Description, at most 60 words, naming no other tool: "Run Python (pandas, numpy, matplotlib) on the passages shown so far, to count, group, compare or chart them. passages picks them by number; left out, every passage shown is used. Tables written as CSV and charts as PNG into out/ are shown to the user."
    - Parameters: `code` (string), `passages` (array of integers), `title` (string, `maxLength` 80); `required: ["code"]`.
  - Refusal texts (model-facing protocol):
    - `call_limit`: "run_python runs once per round; call it again next round." and "run_python has run twice in this turn, the most it may; no more runs in this turn, so answer from what it showed."
    - `no_time`: "not run: too little of this turn's time is left for a run; answer from the passages shown."
    - `no_capacity`: "not run: workbench is busy; call it again in your next round, or answer from the passages shown."
    - `unavailable`: "workbench did not answer; answer from the passages shown."
    - `too_much_data`: "the data was too large for one run; pass fewer passages."
    - `code_failed`: "the code did not finish within its time and memory limits; make it simpler, or answer from the passages shown."
  - `prepare` order: count the round, then the turn, then check the arguments, then the numbers. Only then does it build the files and the provenance, count the turn's call, and return the `ToolWork`.
  - `fetch` order: compute `t`; take the slot without waiting; `record_disclosure`; `client.run_python(... timeout_s=t)`; then `answered(job_id)` or `unanswered()`; give the slot back.
  - Reply priority, all within `ctx.budget.result_chars`:
    1. the head line and `ANALYSIS_NOTE`;
    2. table shapes, chart names, other outputs with their sizes, what was left out, dropped charts, workbench's `hint`;
    3. the tail of `stderr`, at most 500 characters;
    4. up to 5 rows per table, as CSV lines of at most 200 characters;
    5. `stdout`, head and tail around a cut marker, filling what is left.
  - Charts: a chart is a PNG whose IHDR width and height are both at most `MAX_CHART_SIDE`, kept while the caps allow, stored as `data:image/png;base64,…`.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(agent): run_python hands the turn's evidence to workbench and keeps its tables and charts"`

### Task 6: Offering the tool (I3, part 3)

**Files:**
- Modify:
  - `docint/agents/turn/toolsets.py`
  - `docint/core/rag.py`: `agent_turn`, `_offer`, `_agent_system_prompt`, and a new `_agent_python_tool`.
  - `docint/core/state/session_manager.py`: `stream_agent_turn`.
- Create: `docint/utils/prompts/en/agent_tools_python.txt`, `docint/utils/prompts/de/agent_tools_python.txt`
- Test: `tests/test_agent_toolsets.py`, `tests/test_agent_rag.py`, `tests/test_agent_session.py`, `tests/test_prompt_loader_locale.py`

**Interfaces:**
- Consumes: `WorkbenchPythonTool`, `PythonTurn` (Task 5); `workbench_client()` (Task 2).
- Produces:
  - `toolset_for(target, *, search, search_images, list_keyframes, keyword=None, topics=None, python: WorkbenchPythonTool | None = None) -> ToolRegistry`. The python tool goes after `topics` and before `read` on `documents` and `all`, and is ignored on `visual`.
  - `RAG.agent_turn(..., python_turn: PythonTurn | None = None) -> AgentTurn | None`.
    - It builds the tool only when `python_turn` is set, the turn is not scoped, the target is `documents` or `all`, `workbench_client()` is not `None`, and `.discover()` returns a `Discovery`.
    - When room is short it gives up `run_python` first, logging at INFO "The run_python tool was withheld from an agent turn: {} tokens of room with it, under the {} one needs". It then gives up topics, the keyword fields and keyword, as today.
  - `RAG._agent_python_tool(retrieval_target: str, *, scoped: bool, python_turn: PythonTurn | None) -> WorkbenchPythonTool | None`
  - `RAG._agent_system_prompt(..., python: bool = False)` appends the `agent_tools_python` line after the topics line.
  - `SessionManager.stream_agent_turn` passes `python_turn=PythonTurn(owner, session_id, turn_started_at + load_agent_env().time_budget_s)` when `persist and session_id is not None and owner is not None`, else `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_toolsets.py  (_descriptions() now builds "all" with a python tool too)
@pytest.mark.parametrize(("target", "names"), [
    ("documents", ["search", "keyword", "topics", "run_python", "read"]),
    ("all", ["search", "search_images", "keyframes", "keyword", "topics", "run_python", "read"])])
def test_run_python_sits_after_topics_and_before_read(target: str, names: list[str]) -> None:
def test_a_visual_turn_is_never_offered_run_python() -> None:
PYTHON_TOKENS = 400
def test_run_python_costs_a_turn_no_more_than_its_planned_tokens() -> None:  # schema + its guide line in the costlier locale <= PYTHON_TOKENS; shorten the line before raising it
```

```python
# tests/test_agent_rag.py
@pytest.mark.parametrize("case", ["no_turn", "scoped", "visual", "no_client", "not_discovered"])
def test_run_python_is_offered_only_where_its_analyses_can_be_stored(monkeypatch, case: str) -> None:  # "run_python" not in names; otherwise (the base case) it is, with the guide line in the prompt
def test_run_python_is_withheld_first_when_room_is_short(monkeypatch, loguru_caplog_info) -> None:  # one token short: no run_python, topics and keyword with every field kept; one INFO line
def test_the_python_line_follows_the_topics_line(monkeypatch) -> None:  # _agent_system_prompt("all", ..., topics=True, python=True) ends with the two lines in that order
def test_without_workbench_a_turn_offers_what_it_always_did(monkeypatch) -> None:
```

```python
# tests/test_agent_session.py
@pytest.mark.parametrize(("persist", "session_id", "expected"), [
    (True, "session-1", PythonTurn("user-a", "session-1", 100.0 + 120.0)), (False, "session-1", None), (True, None, None)])
def test_only_a_stored_turn_hands_its_owner_session_and_deadline_to_the_tool(persist, session_id, expected) -> None:  # rag.agent_turn double records python_turn and returns None
```

```python
# tests/test_prompt_loader_locale.py
def test_the_run_python_line_exists_in_every_locale() -> None:  # en and de; each names passages.json, keyword_checks.json, topics.json and out/
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_toolsets.py tests/test_agent_rag.py tests/test_agent_session.py tests/test_prompt_loader_locale.py -v -k "python or run_python"`: FAIL.
- [ ] **Step 3: Implement it and write the two prompt lines**
  - The English line, one line, in the style of `agent_tools_topics.txt`: "- run_python(code, passages, title): runs Python on the passages shown so far. Its folder holds passages.json, a list of {n, text, file, page, time, author, network, kind}, and keyword_checks.json and topics.json when this turn made such checks. Write tables as CSV and charts as PNG into out/, at most 4 each, and print a short summary. One call per round, two per turn; there is no network and nothing can be installed. Cite the passages [n] the code used."
  - The German line says the same, with file names, keys and `out/` unchanged.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(agent): offer run_python on stored text turns while workbench is discovered; given up first when room is short"`

### Task 7: Analyses as validator facts (I4)

**Files:**
- Modify:
  - `docint/agents/turn/workbench_tool.py`: `ANALYSIS_CHECKS_CHARS`, `STDOUT_CHECK_CHARS`, `render_analysis_checks`.
  - `docint/agents/types.py`: `RetrievalResult`.
  - `docint/agents/generation.py`
  - `docint/core/api.py`: `_validation_payload`, `_turn_validation`, `_assessment_validation`, and the `cited_sources` fallbacks.
  - The four validator prompts: `response_validator.txt` and `response_validator_assessment.txt` in `docint/utils/prompts/en/` and `de/`.
- Test: `tests/test_agent_workbench_tool.py`, `tests/test_agents_generation.py`, `tests/test_api.py`, `tests/test_prompt_loader_locale.py`

**Interfaces:**
- Consumes: the analysis dict (Task 5), read from a turn payload's `analyses`.
- Produces:
  - `ANALYSIS_CHECKS_CHARS = 1_600`, `STDOUT_CHECK_CHARS = 600`
  - `render_analysis_checks(analyses: Sequence[Mapping[str, Any]], *, max_chars: int = ANALYSIS_CHECKS_CHARS) -> str`
    - It writes one line per analysis, of the form `- Analysis 1 "Posts per author": from passages [1], [2]; tables counts.csv (2 rows × 2 columns); stdout: <first 600 characters on one line>`.
    - It skips malformed entries and defuses titles as `_check_label` does.
    - Lines are kept whole while they fit.
  - `RetrievalResult.analysis_checks: str | None = None`
  - `ANALYSIS_CHECKS_HEADING = "Analysis checks (results of code run on the passages named; facts computed from those passages, not sources):"`
  - `ResultValidationResponseAgent.finalize`: the no-sources guard also counts `analysis_checks`.
  - `_build_prompt(..., analysis_checks: str | None = None)` adds the block after the topic checks. `NO_SOURCES_BESIDE_CHECKS` applies when any check is present.
  - `_validation_payload(..., analysis_checks: str | None = None)`
  - `_assessment_validation(*, question, answer, sources, agent, analyses: Any = None)`
  - `_turn_validation` passes `data.get("analyses")`.
  - `cited_sources(..., fallback=not (checks or topic_checks or analysis_checks))` in both agent modes. `split.py` already takes `fallback` and is unchanged.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_workbench_tool.py
def test_analysis_checks_name_the_title_passages_shapes_and_output() -> None:
    assert render_analysis_checks([ANALYSIS]) == (
        '- Analysis 1 "Posts per author": from passages [1], [2]; tables counts.csv (2 rows × 2 columns); stdout: 2 authors')
def test_analysis_checks_cut_stdout_to_600_and_keep_lines_whole_within_1600() -> None:
def test_a_malformed_analysis_is_skipped() -> None:  # tables not a list, title not a string, provenance missing -> no line, no error
```

```python
# tests/test_agents_generation.py
def test_analysis_checks_count_in_the_no_sources_guard() -> None:  # no sources, analysis_checks set -> the model is asked (not "Answer produced without retrieved sources.")
def test_analysis_checks_show_under_their_own_heading() -> None:  # the prompt holds ANALYSIS_CHECKS_HEADING then the lines; NO_SOURCES_BESIDE_CHECKS without sources
```

```python
# tests/test_api.py  (beside test_an_assessment_hands_its_keyword_checks_to_the_validator)
def test_an_agent_lookup_hands_its_analyses_to_the_validator(monkeypatch) -> None:  # _turn_validation({"answer_mode": "lookup", "analyses": [ANALYSIS]}, ...) -> f"\n{ANALYSIS_LINE}\n" in the prompt
def test_an_assessment_hands_its_analyses_to_the_validator(monkeypatch) -> None:
def test_an_answer_citing_nothing_beside_an_analysis_rests_on_it(monkeypatch) -> None:  # NO_SOURCES_BESIDE_CHECKS in the prompt; the uncited source text absent
```

```python
# tests/test_prompt_loader_locale.py
@pytest.mark.parametrize("name", ["response_validator", "response_validator_assessment"])
@pytest.mark.parametrize("code", ["en", "de"])
def test_the_validator_accepts_a_figure_an_analysis_check_gives(name: str, code: str) -> None:  # "Analysis checks" in the prompt
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_workbench_tool.py tests/test_agents_generation.py tests/test_api.py tests/test_prompt_loader_locale.py -v -k "analys"`: FAIL.
- [ ] **Step 3: Implement it and add one line to each validator prompt**
  - `response_validator.txt` (en), after the topic-check line: "A figure the answer states (a count, a sum, a share, a table cell) counts as grounded when a listed analysis check gives it: the code computed it from the passages the check names, so the answer cites those passages, not the figure."
  - The assessment prompt says "supported" in place of "grounded".
  - The German prompts carry the same sentence, naming the heading „Analysis checks“ as they name „Keyword checks“.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(validation): a turn's analyses reach its validator as computed facts"`

### Task 8: Disclosures in the chat routes (I5, part 2)

**Files:**
- Modify:
  - `docint/core/audit.py`: `DISCLOSE_OPERATION`, `WORKBENCH_RECIPIENT`, `chat_also`, `write_disclosures`, `disclosing`.
  - `docint/core/api.py`: `_CHAT_ALSO`, both chat routes, `_record_chat_turn`.
- Test: `tests/test_audit_core.py`, `tests/test_audit_api.py`

**Interfaces:**
- Consumes:
  - Task 4: `DisclosureLedger`, `DisclosureRecord`, `binding`, `current_ledger`, `record_disclosure`.
  - Task 2: `CHAT_REASON`.
  - Task 1: `load_workbench_env`.
- Produces:
  - `DISCLOSE_OPERATION = "chat.workbench"`, `WORKBENCH_RECIPIENT = "workbench:run_python"`
  - `chat_also(workbench: bool) -> tuple[str, ...]` returns `("ai_call", "disclose")` or `("ai_call",)`. `api.py` sets `_CHAT_ALSO = chat_also(load_workbench_env().enabled)` at import, and both chat routes use `audited("query", "chat.query", also=_CHAT_ALSO)`.
  - `write_disclosures(op: Operation, records: Sequence[DisclosureRecord]) -> None` writes one child per record:
    - action and operation: `op.child("disclose", DISCLOSE_OPERATION, …)`
    - `target=Target(kind="chunk", ids=list(r.point_ids), count=len(r.point_ids))`, `recipient=WORKBENCH_RECIPIENT`, `outcome=r.outcome`
    - criteria `passages`, `keyword_checks`, `topic_listings` and `data_bytes`, plus `workbench_job` when it is known and `incomplete: True` when the record is incomplete.
    - A failed write raises, as every audit write does.
  - `@contextmanager disclosing(op: Operation) -> Iterator[DisclosureLedger]` binds a fresh ledger. On every exit it calls `seal()`. If records remain, it settles an unsettled `op` with `settle_quietly(op, criteria={"incomplete": True})` and writes them beneath its primary or failure event; a write that fails there is logged, never raised over the turn's own outcome. With no record it leaves `op` alone, so a client error still records nothing, as `docint/core/errors.py` states.
  - `_record_chat_turn`: right after `op.record(...)`, it calls `write_disclosures(op, ledger.take())` for the bound ledger, then the `ai_call` child.
  - `/query`: `with forwarding_of(op.context), disclosing(op):` wraps everything from the `rag.collection_scope` block through `_record_chat_turn`.
  - `/stream_query`: `event_generator` enters `forwarding_of(op.context), disclosing(op)` inside its `rag.collection_scope(...)`, `rag.reasoning_scope(...)` block, so both also close when the stream is cancelled.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_audit_core.py
def test_disclosing_writes_what_the_route_left_beneath_its_primary_event(stream) -> None:
def test_disclosing_settles_a_failed_operation_before_its_disclosures(stream) -> None:  # an exception inside -> primary outcome error, criteria incomplete; one disclose child; the exception propagates
def test_a_call_in_flight_at_the_seal_is_incomplete(stream) -> None:  # child outcome error, criteria["incomplete"] is True, no "workbench_job"
def test_nothing_is_disclosed_after_the_seal() -> None:  # record_disclosure after the block raises DisclosureRefused
def test_an_operation_without_disclosures_is_left_to_its_route(stream) -> None:  # the block ends unsettled -> no event at all
def test_chat_routes_ask_for_disclose_only_while_workbench_is_configured() -> None:  # chat_also(True) == ("ai_call", "disclose"); chat_also(False) == ("ai_call",)
```

```python
# tests/test_audit_api.py — _PythonRAG(DummyRAG): its chat() and stream_chat() call record_disclosure(point_ids=["1", "2"],
# passages=2, keyword_checks=1, topic_listings=0, data_bytes=2048).answered("0" * 32) before answering; installed as api_module.rag
def test_a_turn_that_ran_python_records_one_disclose_per_call(client, audit_stream) -> None:
    query, disclose, answer = _ops(audit_stream)
    assert [(e["action"], e["operation"]) for e in (query, disclose, answer)] == [
        ("query", "chat.query"), ("disclose", "chat.workbench"), ("ai_call", "chat.answer")]
    assert disclose["parent_event_id"] == query["event_id"] and disclose["recipient"] == "workbench:run_python"
    assert disclose["target"] == {"kind": "chunk", "ids": ["1", "2"], "count": 2, "container": "alpha"}
    assert {k: disclose["criteria"][k] for k in ("workbench_job", "passages", "keyword_checks", "topic_listings", "data_bytes")} == {
        "workbench_job": "0" * 32, "passages": 2, "keyword_checks": 1, "topic_listings": 0, "data_bytes": 2048}
    assert (disclose["purpose"], disclose["request_id"]) == (query["purpose"], query["request_id"])

def test_a_streamed_turn_discloses_before_its_final_frame(client, audit_stream) -> None:
def test_a_call_that_got_no_answer_is_disclosed_as_an_error(client, audit_stream) -> None:  # .unanswered() -> outcome error, no workbench_job
def test_a_failed_turn_writes_its_disclosures_beneath_the_failure_event(client, audit_stream) -> None:  # chat raises after recording -> 500; query outcome error; the disclose child under it
def test_a_stream_whose_client_went_away_discloses_every_call_sent_and_sends_no_more(client, audit_stream, monkeypatch) -> None:
    # Request.is_disconnected -> True; stream_chat records one disclosure in flight, waits past STREAM_DISCONNECT_POLL_INTERVAL_S,
    # then records again: that second record raises DisclosureRefused (captured). Events: query error with incomplete,
    # one disclose child with outcome error and criteria incomplete, and no workbench_job.
def test_calls_to_workbench_state_the_turns_purpose(client) -> None:  # chat() captures forward_headers(system_reason=CHAT_REASON): case-0001/review with the header; reason "docint chat" without one
def test_a_client_error_still_records_nothing(client, audit_stream) -> None:  # a 409 session/collection mismatch -> no chat event
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_audit_core.py tests/test_audit_api.py -v -k "disclos or workbench or purpose or client_error"`: FAIL.
- [ ] **Step 3: Implement it as listed.**
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green. The autouse `audit_stream` fixture verifies every chain.
- [ ] **Step 5: Commit** — `git commit -m "feat(audit): one disclose per run_python call, written with the turn or at its seal"`

### Task 9: Storing analyses and the analysis route (I6)

**Files:**
- Create: `docint/core/state/turn_analysis.py`
- Modify:
  - `docint/core/state/turn.py`: `analyses` relationship.
  - `docint/core/state/__init__.py`: register `TurnAnalysis`.
  - `docint/core/state/session_manager.py`: `_persist_turn`, `_finish_agent_turn`, `get_session_history`, `get_analysis`, `find_analysis`.
  - `docint/core/api.py`: `QueryOut.analyses`; both routes serve `analyses`; the new route.
- Test: `tests/test_agent_persistence.py`, `tests/test_session_history_collection_scope.py`, `tests/test_api.py`, `tests/test_audit_api.py`

**Interfaces:**
- Consumes: `AgentResult.analyses` (Task 3); the analysis dict (Task 5).
- Produces:
  - `class TurnAnalysis(Base)`, `__tablename__ = "turn_analyses"`. Columns:
    - `id: Mapped[str]` (String primary key, uuid4 hex minted before the write)
    - `turn_id: Mapped[int]` (FK `turns.id`, indexed)
    - `position: Mapped[int]`
    - `payload: Mapped[str]` (JSON text)
    - `created_at`
    - `create_all` makes the table; no migration is needed.
  - `Turn.analyses = relationship("TurnAnalysis", cascade="all, delete-orphan", order_by="TurnAnalysis.position")`
  - `stamp_analysis(payload: Mapping[str, Any], *, analysis_id: str, question: str, session_id: str, turn_idx: int) -> dict[str, Any]` adds `id`, `question`, `provenance.session_id` and `provenance.turn_idx`.
  - `served_analysis(payload: Mapping[str, Any], collection: str | None) -> dict[str, Any]` returns a copy with `provenance.collection` set to the caller's logical collection.
  - `analysis_summary(payload: Mapping[str, Any], index: int) -> dict[str, Any]` returns `{"id", "index", "title", "tables": [{"name", "columns": int, "rows": int}], "charts": [{"name", "bytes"}]}`.
  - `SessionManager._persist_turn(..., analyses: Sequence[Mapping[str, Any]] = ()) -> int`. It keeps its return value: 60 test call sites read the index. It writes the stamped rows with the turn. A replaced turn clears its analyses as it clears citations.
  - `_finish_agent_turn` mints the ids only when it persists. Its final payload gains `analyses`: the stamped payloads, or `[]`.
  - `SessionManager.get_analysis(session_id: str, analysis_id: str, owner: str | None) -> dict[str, Any] | None`, owner-scoped.
  - `SessionManager.find_analysis(analysis_id: str, owner: str | None) -> tuple[dict[str, Any], str | None] | None` returns the payload and the session's physical collection.
  - History: each assistant message with analyses gains `analyses: [analysis_summary(...)]`.
  - `QueryOut.analyses: list[dict[str, Any]] = []`. `/query` returns, and the `/stream_query` final frame carries, `[served_analysis(a, payload.collection) for a in …]`.
  - `GET /sessions/{session_id}/analyses/{analysis_id}?collection=`, tag Sessions, `audited("query", "session.analysis")`.
    - It is gated like the history route: an unknown id, another owner, or another collection is 404 `{"detail": "Analysis not found."}`.
    - It records `Target(kind="analysis", ids=[analysis_id], count=1)` with criteria `{"session_id": …}`.
    - It returns `served_analysis(...)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_persistence.py
def test_a_turns_analyses_are_stored_with_it(session_manager) -> None:
    final = _finish(session_manager, AgentResult(..., analyses=[ANALYSIS]))
    [stored] = final["analyses"]
    assert len(stored["id"]) == 32 and stored["question"] == MESSAGE
    assert (stored["provenance"]["session_id"], stored["provenance"]["turn_idx"]) == ("session-1", 0)
    assert session_manager.get_analysis("session-1", stored["id"], "user-a") == stored
def test_a_replaced_turn_clears_its_analyses(session_manager) -> None:
def test_analyses_go_with_their_conversation(session_manager) -> None:  # delete_session -> no turn_analyses row left
def test_a_stateless_turn_stores_no_analysis(session_manager) -> None:  # persist False -> final["analyses"] == []
def test_history_summarizes_each_analysis(session_manager) -> None:  # {"id", "index": 1, "title", "tables": [{"name": "counts.csv", "columns": 2, "rows": 2}], "charts": [{"name": "chart.png", "bytes": 48213}]}; no data_uri
```

```python
# tests/test_session_history_collection_scope.py
def test_an_analysis_is_found_only_by_its_owner(session_manager) -> None:  # get_analysis/find_analysis for another owner -> None; find_analysis returns the pinned collection
```

```python
# tests/test_api.py  (beside test_session_history_is_read_only_from_its_own_collection)
def test_an_analysis_is_read_only_from_its_own_session_and_collection(client) -> None:  # 200 with provenance.collection == "docs"; another owner, another collection, an unknown id -> 404
def test_a_turns_final_frame_and_body_carry_its_analyses(client) -> None:  # /query JSON and /stream_query's final frame: analyses with id and the logical collection
```

```python
# tests/test_audit_api.py
def test_reading_an_analysis_is_a_query_of_kind_analysis(client, audit_stream) -> None:  # ("query", "session.analysis"), target {"kind": "analysis", "ids": [ID], "count": 1, ...}
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_persistence.py tests/test_session_history_collection_scope.py tests/test_api.py tests/test_audit_api.py -v -k "analys"`: FAIL.
- [ ] **Step 3: Implement it as listed.** The route matches the history route's gate: `ensure_collection_owner_manager().resolve(owner, collection)` must equal the session's pinned collection. `/sessions/` is already proxied by nginx and the dev proxy.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(sessions): analyses stored with their turn; GET /sessions/{id}/analyses/{analysis_id}"`

### Task 10: Adding an analysis to a report (I7, part 1)

**Files:**
- Modify:
  - `docint/core/api.py`: `add_report_item`, `add_report_items`, and a new `_analysis_item`.
  - `docint/core/retention_sources.py`: `report_item_sources`.
- Test: `tests/test_api_reports.py`, `tests/test_derived_expiry.py`

**Interfaces:**
- Consumes: `SessionManager.find_analysis`, `served_analysis` (Task 9).
- Produces:
  - `_ANALYSIS_CHART = re.compile(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}")`, matched with `fullmatch`.
  - `_analysis_item(raw: Mapping[str, Any], report: Mapping[str, Any], principal: Principal) -> tuple[str, dict[str, Any]]` returns `(f"analysis:{id}", served_analysis(stored, report["collection_name"]))`. It raises 404 `{"detail": "Analysis not found."}` in each of these cases:
    - `analysis_id` is missing or not a string;
    - the row is not the caller's (`principal.effective_owner`);
    - the session's physical collection is not the report's resolved one;
    - a chart fails `_ANALYSIS_CHART`, is over 512 KiB decoded, or the charts are over 1 MiB together.
  - Both add routes use it for `artifact_type == "analysis"`. They ignore the client's `dedupe_key` and every other snapshot field, and skip thumbnail enrichment. In the batch route, one failing item fails the batch with 404. The audit stays `modify` / `report.item_add` (`report.items_add`).
  - `report_item_sources("analysis", snapshot)` returns `(hashes from provenance.sources[].file_hash, set(), not hashes)`.

- [ ] **Step 1: Write the failing tests**

`_ReportRAG` gains `ensure_session_manager()`, a real `SessionManager` on the same StaticPool engine. `_stored_analysis(owner, physical) -> str` writes a conversation, a turn and one `TurnAnalysis` through the ORM.

```python
def test_an_analysis_is_added_from_its_stored_row(client) -> None:
    item = client.post(f"/reports/{rid}/items", params={"collection": "docs"}, json={
        "artifact_type": "analysis", "dedupe_key": "anything", "snapshot": {"analysis_id": aid, "title": "forged"}}).json()
    assert item["dedupe_key"] == f"analysis:{aid}"
    assert item["snapshot"]["title"] == STORED_TITLE and item["snapshot"]["provenance"]["collection"] == "docs"
@pytest.mark.parametrize("case", ["other_owner", "other_collection", "unknown_id", "no_id", "svg_chart", "chart_over_cap", "charts_over_total"])
def test_anything_else_is_not_found(client, case: str) -> None:  # 404; the report holds no item
def test_the_batch_route_builds_analyses_the_same_way(client) -> None:  # one good and one foreign analysis -> 404, nothing added; two good ones -> added 2
```

```python
# tests/test_derived_expiry.py (extend test_report_item_sources)
assert report_item_sources("analysis", {"provenance": {"sources": [{"file_hash": "a"}, {"file_hash": "b"}]}}) == ({"a", "b"}, set(), False)
assert report_item_sources("analysis", {"provenance": {"sources": [{"file_hash": None}]}}) == (set(), set(), True)
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_api_reports.py tests/test_derived_expiry.py -v -k "analys or report_item_sources"`: FAIL.
- [ ] **Step 3: Implement it as listed.**
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green. `test_api_reports.py`'s route enumeration still passes, since no route was added.
- [ ] **Step 5: Commit** — `git commit -m "feat(reports): an analysis is added by id; the server builds its snapshot from the stored row"`

### Task 11: Rendering the analysis section (I7, part 2)

**Files:**
- Modify: `docint/core/state/report_render.py`, `docint/utils/ui_strings.py`
- Test: `tests/test_report_render.py`

**Interfaces:**
- Consumes: the served analysis snapshot (Task 10); `safe_name` (`docint/core/extract/bundle.py`); `_md_cell`, `_csv_text`, `_html_evidence_figure`, `_TableBudget`.
- Produces:
  - Section registration:
    - `ARTIFACT_ANALYSIS = "analysis"`
    - `SECTION_ORDER` gets `(ARTIFACT_ANALYSIS, "report_section_analyses")` between chat and entities.
    - `SECTION_ANCHOR[ARTIFACT_ANALYSIS] = "sec-analyses"`; `_NUMBERED_SECTIONS` includes it.
  - `ANALYSES_COLUMNS = ("session_id", "turn_idx", "title", "question", "job_id", "workbench_version", "sources", "tables", "charts")`
  - `_fence(text: str, *, info: str = "") -> tuple[str, str]` returns the opening and closing fence: a backtick run one longer than the longest run in `text`, and at least three.
  - Per-format renderers:
    - `_md_analysis(snap: dict[str, Any], note: str | None, number: int) -> list[str]`: `### #n · title`, the question, the code in a `python` fence, `stdout` fenced, each table as a GFM table through `_md_cell` under its name and shape, each chart as `![<name escaped>](data:image/png;base64,…)`, and one provenance line.
    - `_html_analysis(snap: dict[str, Any], note: str | None, number: int, table_budget: _TableBudget) -> str`.
      - A table whose rows × columns fit `table_budget.cells_left`, with at most 32 columns, renders as `<table class="analysis">` and is charged.
      - Any other table falls back to its CSV in `<pre class="csv">`, and the budget stays as it was.
      - Charts go through `_html_evidence_figure` and must match the PNG data-URI rule.
      - Everything is escaped.
    - Zip: `analyses.csv` (`ANALYSES_COLUMNS`, text cells through `_csv_text`), `analysis-<n>/<safe_name(table)>` re-serialized with every cell through `_csv_text`, and `analysis-<n>/<safe_name(chart)>` holding the decoded PNG. Two names that collide after `safe_name` get ` (2)` before the extension.
    - JSON is unchanged.
  - `ui_strings` keys in `en` and `de`:
    - `report_section_analyses` ("Analyses" / "Analysen")
    - `report_label_code` ("Code")
    - `report_label_output` ("Output" / "Ausgabe")
    - `report_label_computed_from` ("Computed from" / "Berechnet aus")
    - `report_label_workbench_job` ("workbench {version}, job {job}" / "workbench {version}, Job {job}")
    - `report_label_table_shape` ("{rows} rows × {columns} columns" / "{rows} Zeilen × {columns} Spalten")
    - `report_label_analysis_untitled` ("Analysis" / "Analyse")

- [ ] **Step 1: Write the failing tests**

```python
def test_analyses_render_between_chat_and_entities_numbered() -> None:  # md and html section order; anchor id="sec-analyses"; "#1"; TOC entry "Analyses (1)"
def test_md_renders_code_output_tables_charts_and_provenance() -> None:
    md = render_markdown(_report(ANALYSIS_ITEM))
    assert "```python\nimport pandas as pd" in md and "| author | posts |" in md and "| user-a | 12 |" in md
    assert "![chart.png](data:image/png;base64," in md and "harbour-report.pdf" in md and "0.2.0" in md
def test_md_escapes_a_tables_cells_and_a_charts_name() -> None:  # cell "a|b" -> "a\\|b"; chart name "x](y" -> alt text escaped, nothing after the data URI
def test_md_fences_outlast_the_backticks_inside() -> None:  # Review Focus 4: code with "```" -> fenced by "````"; stdout with "````" -> "`````"
def test_html_renders_a_table_within_the_budget_and_falls_back_past_it() -> None:  # a 2×2 table -> <table class="analysis">; a 500×32 table -> <pre class="csv">
def test_one_budget_spans_chat_answers_and_analyses() -> None:  # a chat answer whose Markdown table takes 9 990 cells -> the analysis's 2×2+ table falls back
def test_html_escapes_cells_and_drops_a_chart_that_is_not_a_png() -> None:  # cell "<script>" escaped; chart "data:image/svg+xml;…" absent
def test_the_zip_holds_the_analyses_index_their_tables_and_charts() -> None:
    names = zipfile.ZipFile(io.BytesIO(report_csv_bundle(_report(ANALYSIS_ITEM)))).namelist()
    assert {"analyses.csv", "analysis-1/counts.csv", "analysis-1/chart.png"} <= set(names)
    # chart.png bytes == the decoded data URI; a cell "=SUM(A1)" reads "'=SUM(A1)"; table "../x.csv" -> "analysis-1/x.csv"
def test_json_keeps_the_stored_snapshot() -> None:
def test_the_pdf_fetches_only_data_uris(monkeypatch) -> None:  # as the existing PDF tests: skip without WeasyPrint; renders with the chart; no other URL fetched
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_report_render.py tests/test_ui_strings.py -v -k "analys or fence or zip"`: FAIL.
- [ ] **Step 3: Implement it as listed.** The PDF path needs no change: it renders `render_html`.
- [ ] **Step 4: Run them to verify they pass**, then `uv run pre-commit run --all-files && uv run pytest`: green. `test_ui_string_table_is_complete_across_languages` covers the new keys.
- [ ] **Step 5: Commit** — `git commit -m "feat(reports): the analysis section in md, html, pdf and zip"`

### Task 12: Frontend and `AgentAction` (I8)

**Files:**
- Modify:
  - `docint/agents/types.py`: `AgentAction` gains `"run_python"`.
  - `tests/test_agent_wire.py`: the pinned set.
  - `frontend/src/api/types.ts`: `AgentAction`, `ArtifactType`, the analysis types, `ChatFinalEvent.analyses`, `SessionMessage.analyses`.
  - `frontend/src/components/chat/ChatTurn.tsx`: the `run_python` label and its `PlayIcon` marker, and an `AnalysisCard` per analysis below the answer.
  - `frontend/src/routes/Chat.tsx`: the history mapping carries `analyses`.
  - `frontend/src/api/sessions.ts`, `frontend/src/hooks/useSessions.ts`, `frontend/src/lib/reportSnapshots.ts`
  - `frontend/src/routes/Report.tsx`: the section and the item. `components/report/ReportSection.tsx` is the generic foldable section and needs no change.
  - `frontend/src/i18n/en.ts`, `de.ts`
- Create: `frontend/src/components/chat/AnalysisCard.tsx`, `frontend/src/components/chat/AnalysisCard.test.tsx`
- Test: `frontend/src/components/chat/ChatTurn.test.tsx`, `frontend/src/routes/Report.test.tsx`, `tests/test_agent_wire.py`

**Interfaces:**
- Consumes: the final frame's `analyses` and the history summaries (Task 9); `GET /sessions/{id}/analyses/{analysis_id}?collection=`; `artifact_type: "analysis"` (Task 10).
- Produces:
  - Types in `types.ts`:
    - `AnalysisTable { name; columns: string[]; rows: string[][]; row_count: number; truncated: boolean }`
    - `AnalysisChart { name; data_uri; bytes }`
    - `AnalysisSource { citation_index; chunk_id; file_hash: string | null; filename: string | null; page: number | null }`
    - `Analysis { id; schema: 1; title; question; code; stdout; tables; charts; provenance: { session_id; turn_idx; collection: string | null; sources: AnalysisSource[]; inputs: {passages; keyword_checks; topic_listings}; workbench: {job_id; version; tool}; created_at } }`
    - `AnalysisSummary { id; index; title; tables: {name; columns: number; rows: number}[]; charts: {name; bytes}[] }`
    - `ChatFinalEvent.analyses?: Array<Analysis | AnalysisSummary>`; `SessionMessage.analyses?: AnalysisSummary[]`
    - `ArtifactType` gains `'analysis'`.
  - `getSessionAnalysis(sessionId: string, analysisId: string, collection: string): Promise<Analysis>`, via `apiGet`
  - `useSessionAnalysis(sessionId: string | null, analysisId: string, enabled: boolean)` (react-query, keyed on the collection)
  - `analysisReportItem(id: string): ReportItemInput` returns `{ artifact_type: 'analysis', dedupe_key: \`analysis:${id}\`, snapshot: { analysis_id: id } }`
  - `AnalysisCard({ entry, index, sessionId, inReport }: { entry: Analysis | AnalysisSummary; index: number; sessionId: string | null; inReport: boolean })`
    - A summary is fetched lazily.
    - The card shows the title (else "Analysis {n}") and a table preview of the first 5 rows, with a "{count} more rows" line.
    - Charts open the preview dialog through `openPreview({ filename, data_uri })`.
    - The code sits behind `DisclosureButton`.
    - A provenance line, and `AddToReportButton`.
  - `Report.tsx` `sections()` order: summary, topics, chat_answer, analysis, entity_finding, hate_speech_finding. An analysis item has its title, its tables' shapes as the body, and its charts as `EvidenceFigure`s.
  - i18n keys in `en` and `de`:
    - `chat.agent_trail_run_python` ("Ran Python: {title}") and `chat.agent_trail_run_python_untitled` ("Ran Python on the passages")
    - `chat.analysis_label` ("Analysis {n}")
    - `chat.analysis_code_show` / `chat.analysis_code_hide`
    - `chat.analysis_more_rows` ("{count} more rows")
    - `chat.analysis_provenance` ("Computed from {sources} · workbench {version}, job {job}")
    - `chat.analysis_loading`, `chat.analysis_failed`
    - `report.section_analyses` ("Analyses")

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_agent_wire.py — the pinned set gains "run_python"
```

```tsx
// ChatTurn.test.tsx
it('labels a run_python step and draws it with PlayIcon', ...)  // "Ran Python: Posts per author"; marker markup === innerMarkup(<PlayIcon />)
it('shows each analysis below the answer', ...)                 // meta.analyses [ANALYSIS] -> "Posts per author" after the answer text
// AnalysisCard.test.tsx
it('shows the title, the first five rows and every chart', ...) // 7 rows -> 5 shown + "2 more rows"
it('keeps the code behind a disclosure', ...)                   // code hidden until "Show code"; then visible
it('opens a chart in the preview dialog', ...)                  // useUiStore previewModal == { filename: 'chart.png', data_uri }
it('names its provenance', ...)                                 // "[1] harbour-report.pdf" and "workbench 0.2.0"
it('adds itself to the report by id alone', ...)                // POST body { artifact_type: 'analysis', dedupe_key: 'analysis:a1', snapshot: { analysis_id: 'a1' } }
it('fetches a summary from history once, through apiFetch', ...) // fetch called once with /sessions/session-1/analyses/a1?collection=docs
// Report.test.tsx
it('renders analyses between chat answers and entity findings, charts as figures', ...)
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_agent_wire.py -v` and `cd frontend && pnpm test -- ChatTurn AnalysisCard Report`: FAIL.
- [ ] **Step 3: Implement it as listed.** Everything goes through `apiFetch`, and the icons come from `@infra/ui` (`PlayIcon`, `DisclosureButton`, `ReportIcon`).
- [ ] **Step 4: Verify** — `uv run pre-commit run --all-files && uv run pytest`, then `cd frontend && pnpm test && pnpm build`: green.
- [ ] **Step 5: Commit** — `git commit -m "feat(spa): run_python in the trail; analysis cards below the answer and in the report"`

### Task 13: Documentation and release (I9)

**Files:**
- Modify: `docs/retrieval-and-agents.md`, `docs/reports.md`, `docs/audit.md`, `docs/configuration.md`, `docs/api-reference.md`, `pyproject.toml`, `uv.lock`

**Interfaces:**
- Consumes: everything above. These are short additions to pages that exist, with no new page.

- [ ] **Step 1: Write the additions**
  - `docs/retrieval-and-agents.md`:
    - § Tools offered gets one `run_python` bullet: where it is offered, and that it is given up first.
    - A new `### Python on the evidence`, of about ten lines, covers: the files and their caps; one call per round and two per turn; the deadline arithmetic; the reply; what becomes an analysis; and the validator facts.
  - `docs/reports.md`: a short `analysis` paragraph covering the server-built snapshot and the section in each format, plus one line in § Exporting for the zip members.
  - `docs/audit.md`:
    - The `POST /query` row adds `disclose` `chat.workbench` per call, with its target and criteria.
    - A `GET /sessions/{session_id}/analyses/{analysis_id}` row: `query` `session.analysis`, `analysis` [id].
    - The SPA table adds the same route: `query`, yes, `apiGet`.
    - A `## Calls to workbench` section after § Calls to Nextext, of about eight lines, covers: the pseudonyms, the forwarded purpose (`docint chat`), the request id, the job id that ties both trails, and the seal. The Contents list gets its entry.
  - `docs/configuration.md`: rows for `WORKBENCH_API_KEY`, `WORKBENCH_API_BASE`, `WORKBENCH_TIMEOUT` and `WORKBENCH_NET`.
  - `docs/api-reference.md`: the route, and `analyses` on `POST /query`, on `/stream_query`'s final frame and on the history.
- [ ] **Step 2: Bump the version** — `pyproject.toml` gets `version = "2.16.0"`, then run `uv lock`.
- [ ] **Step 3: Verify** — `uv run pre-commit run --all-files`, `uv run pytest`, `cd frontend && pnpm test && pnpm build`, `python3 -B ../deploy/scripts/check_env_examples.py .`: all green. The env check is clean once deploy D1 has merged; before that, its one finding is `T5 WORKBENCH_API_KEY`.
- [ ] **Step 4: Commit** — `git commit -m "docs: run_python, the analysis artifact and calls to workbench; release v2.16.0"`

## Final verification

Run from docint's root, quoting each command's output in the PR:

```bash
uv run pre-commit run --all-files
uv run pytest
cd frontend && pnpm test && pnpm build
python3 -B ../deploy/scripts/check_env_examples.py .
```

Expected: all four green; the env check is clean once deploy D1 is on `main`. Open the PR from `feat/workbench-analysis`, and confirm that CI ran on the latest push. Merge after deploy D1. The end-to-end checks against a running workbench (spec §8, 1 to 12) are the owner's step once this PR and workbench v0.2.0 have both merged.
