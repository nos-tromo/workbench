# workbench v0.2.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give workbench a second caller, docint, through a hand-written, stateless, JSON-only MCP door that serves an inline `run_python`: the data comes with the call, lives only in the job, and the outputs go back in the answer, never kept. Open WebUI's terminal door stays as it is.

**Architecture:** Each caller gains a door, a file form, a wall cap and a slot cap (`workbench/api/auth.py`). A tool registry (`registry.py`) describes every tool by its form. The MCP door (`mcp.py` dispatches, `mcp_wire.py` holds the pure wire rules) runs the inline specs (`tools_inline.py`) through the same job queue. The protocol carries the data (`InlineFile`), the supervisor writes it into the job folder (`_write_inline`), and the queue reads the outputs back from staging (`JobQueue._take`) for a taker that records the event before anything goes out.

**Tech Stack:** as v0.1.1 (Python 3.12, uv, FastAPI, uvicorn, pydantic v2, SQLite). The dev group adds `mcp` (MIT, the official SDK), used for one conformance test.

**Spec:** `docs/superpowers/specs/2026-11-15-v2-mcp-docint-design.md`. §3 is binding here. §4 shows what docint expects of the door: the request and answer shapes, the headers and the reason tokens of §3.3–§3.4. Where this plan is silent, the v1 design and `docs/superpowers/plans/2026-11-13-workbench-v1.md` still hold.

## Global Constraints

- Python `>=3.12,<3.13`. The base images stay as they are, pinned by tag and digest.
- No GPL or AGPL dependency. `mcp` (MIT) goes in the `dev` group only and never into an image: the runtime stages of both Dockerfiles sync with `--no-dev`.
- Airgap: nothing fetches anything at runtime. The SDK test runs on loopback.
- Hardening (deploy ADR 0001) is unchanged. Compose adds `ulimits: core: 0` to `api` and `executor`, and jobs keep `RLIMIT_CORE` 0.
- workbench never joins `data-net` or `inference-net`, and the executor keeps `network_mode: none`.
- No caller sees another caller's workspaces. Users are `(caller, user_id)` in the queue and in the job table. docint has no folder under `workspaces/`.
- Audit is by reference only. Never recorded: the data or a digest of it, file names, the code, `stdout`, the text of a refusal. The vendored `workbench/utils/audit_event.py`, `workbench/utils/audit_runtime.py`, `make/common.mk` and `scripts/bundle-lib.sh` stay verbatim.
- Logs hold counts and types only. No string a client chose reaches a log line: not a header, and not a JSON-RPC method the door does not serve.
- Fail closed: if an event cannot be written, nothing of the answer goes out.
- Identity travels only in the headers `X-User-Id`, `X-Session-Id` (`default` without one), `X-Audit-Purpose` and `X-Request-ID`, never in `_meta`. No parameter uses `x-mcp-header`. No `Mcp-Session-Id` is ever minted; one that is sent is ignored, and so is `Last-Event-ID`.
- Callers (§3.1):

  | Caller | Door | Files | Tools | Wall cap | Running jobs |
  |---|---|---|---|---|---|
  | `open-webui` | `terminal` | `workspace` | all nine | `WORKBENCH_JOB_WALL_S` | not capped |
  | `docint` | `mcp` | `inline` | `DOCINT_WORKBENCH_TOOLS` (`run_python`) | `DOCINT_WORKBENCH_WALL_S` (60) | `DOCINT_WORKBENCH_SLOTS` (2) |

- Revisions: `2026-07-28` is modern; `2025-11-25` and `2025-06-18` are legacy; any other is refused. An `initialize` that names another revision gets `2025-11-25`.
- Inline request (§3.4):
  - `code`: 1 to 50 000 characters.
  - `data`: 0 to 8 entries `{name, content}`. Each `name` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.(json|csv|txt|md)$` and is unique. All `content` together is at most 512 KiB in UTF-8.
  - `time_limit_s`: optional, from 5 to the caller's wall cap. A value outside is refused, never clamped.
  - An unknown field is refused.
- Returns (§3.4):
  - Types: only `.png`, `.csv`, `.txt`, `.md` and `.json`.
  - Order: `.png`, then `.csv`, then the rest, each group by name.
  - Caps: at most 8 files; an image up to 512 KiB, a text up to 256 KiB; 1 MiB raw in all.
  - Checks: a PNG must start with the PNG signature; a text must be strict UTF-8.
- Limits (§3.5):
  - A job's wall time is the least of `time_limit_s`, the caller's `wall_s` and `WORKBENCH_JOB_WALL_S`; the code gets 9/10 of it.
  - An MCP call waits at most `MCP_QUEUE_WAIT_S` = 5 seconds.
  - docint's waiting calls are capped at `slots × (WORKBENCH_QUEUE_MAX // WORKBENCH_EXECUTOR_SLOTS)`: 8 of 16 by default.
- The reason tokens are the contract, the texts are not: `invalid_arguments`, `queue_full`, `wait_timeout`, `executor_busy`, `too_large`, `not_completed`, `refused`, `purpose_required`, and the `data.reason` values `audit_unavailable` and `internal`.
- Open WebUI's `run_python` is unchanged: its route, schema and description, its kept outputs and its audit operation. Its tool document keeps its nine operations.
- Synthetic data only (`user-a`, `chat-1`, `passages.json`, `harbour-report.pdf`), and no paths from a developer's machine: in code, tests, docs and commit messages.
- Documentation is only the short additions Task 7 lists (owner decision 7).
- Before every commit, `make verify` and `make test` pass (`git add` new files first).

## Review Focus

1. **JSON-RPC ids of every allowed shape.** Clients send string ids (`"req-1"`) as often as integers, and `0` is a valid id. Expected: each answer echoes the id with its type. A `null`, boolean, float, array or object id is `400`, `-32600`, with no `id` in the answer. Test: Task 6 `test_the_id_comes_back_as_sent`.
2. **Data that escaping inflates.** 512 KiB of quote-heavy text passes the UTF-8 bound, but it doubles on the executor's 1 MiB line. Expected: an `isError` result with `too_large`, never a 500, no event, and nothing left in `jobs/` or `staging/`. Test: Task 5 `test_escape_heavy_data_is_too_large_never_a_500`.
3. **Output names a model really writes:** spaces, umlauts, `#`, `%`, or an upper-case `Chart.PNG`. Expected: the name comes back exactly in `files[].name`, and percent-encoded in the resource URI. An upper-case suffix still counts as its type. Tests: Task 4 `test_upper_case_suffixes_come_back` and Task 5 `test_a_uri_holds_the_name_percent_encoded`.
4. **Text that is not UTF-8.** `df.to_csv("out/t.csv", encoding="latin-1")` with "Müller" in it. Expected: the file is dropped and counted, and the hint says to write UTF-8. A UTF-8 file with a BOM comes back. Test: Task 4 `test_a_png_needs_its_signature_and_text_must_be_strict_utf8`.
5. **Headers as other clients write them:** `Application/JSON; charset=UTF-8`, or no `Accept` at all. Expected: media types match case-insensitively with their parameters, and a missing `Accept` counts as `*/*`. Test: Task 6 `test_media_types_are_matched_as_http_says`.

## Calls this plan makes (design; revisit with a stated reason)

- **Regrouping.** W5 and W6 are one task (Task 5): the inline tool's taker writes W6's event, and the spec tests §3.4 and §3.6 together in `test_tools_inline.py`. `require_tool` lands in Task 2 with the routes that use it. `mcp` joins the dev group in Task 6 with the SDK test, so Task 9 only bumps the version.
- **docint's rules.** They are checked only while `DOCINT_WORKBENCH_KEY` is set, so an Open WebUI-only host with `WORKBENCH_EXECUTOR_SLOTS=2` still starts. docint's wall cap must also be at least 5, the schema's minimum.
- **The start check.** It reads two static sets: `registry.WORKSPACE_TOOLS` and `registry.INLINE_TOOLS`. A test ties each set to the specs it names. A docint key is therefore usable from Task 1 on.
- **`ToolSpec.input_schema`.** This field is added because `run_python`'s schema depends on the caller.
- **`JobQueue.submit_inline`.** It is a separate method on the same admission path, so each method has one typed return.
- **The protocol.** It refuses `inline_inputs` together with `input_files`, so no inline name can shadow a copied input.
- **What is recorded.** An event is written for a job the executor answered, `ok` or not, and the `denied` event for a missing purpose. A call refused before that leaves no event, as v1's busy calls do: invalid arguments, the queue, a busy executor, a request too large.
- **The purpose check.** It runs inside `run_python_inline`, after the door has validated the arguments.
- **Suffixes.** They match case-insensitively. `files` lists every output, with `returned` true or false.
- **Notifications.** A notification gets `202` after the shape check, whatever its method; no version check applies to it.
- **Other wire refusals.** `403`, `405`, `406` and `415` carry a JSON-RPC error `-32600` with no id.
- **The `_meta` keys.** The client keys use the prefix `io.modelcontextprotocol/`, as `protocolVersion` and `serverInfo` do. Legacy results carry no `resultType`, `ttlMs` or `cacheScope`.

## File structure

| File | Responsibility |
|---|---|
| `workbench/config.py` | the docint variables, `AUDIT_REQUIRE_PURPOSE`, either key |
| `workbench/api/auth.py` | `Caller` rows, the door check in `KeyGate`, `require_tool`, `principal_from_headers` |
| `workbench/api/registry.py` (new) | `ToolSpec`, `ToolEnv`, `ToolOutcome`, `Registry`, the tool sets per form |
| `workbench/api/errors.py` | `describe`, `compact` |
| `workbench/api/openapi.py` | one tool document per caller |
| `workbench/protocol.py` | `InlineFile`, `JobRequest.inline_inputs` |
| `workbench/executor/supervisor.py` | `Supervisor._write_inline` |
| `workbench/api/jobs.py` | `CallLimits`, `_CallerGate`, `submit_inline`, `_take`, `Taker` |
| `workbench/api/tools.py` | the `run_python` helper both forms use |
| `workbench/api/tools_inline.py` (new) | `InlinePythonRequest`, `InlinePythonResult`, `run_python_inline`, `INLINE_SPECS` |
| `workbench/api/audit.py` | `run_python_inline`; the target kind, count and request id of `record` |
| `workbench/api/mcp_wire.py` (new) | revisions, header and media checks, JSON-RPC shapes; no I/O |
| `workbench/api/mcp.py` (new) | `POST /mcp`, the dispatch, mapping refusals to answers |
| `workbench/api/body_limit.py`, `main.py` | the JSON-RPC `413`; the router, the registry, `config.audit` |

## Branch and pull request

One branch, `feat/v2-mcp-door`, cut from `main` at execution time, with one commit per task. Open the PR after Task 9 and confirm that CI ran on its last push. Merge it only after deploy's D1 PR has merged: until D1 adds its `env-sync.conf` rows, `check_env_examples.py` reports `T5` for `DOCINT_WORKBENCH_KEY`'s `[shared: docint WORKBENCH_API_KEY]` tag.

---

### Task 1: Callers and config (W1)

**Files:**
- Modify: `workbench/config.py`, `workbench/api/auth.py`, `tests/test_config.py`, `tests/api/conftest.py`
- Create: `tests/api/test_callers.py`

**Interfaces:**
- Consumes: `audit_runtime.parse_required_actions`, `audit_event.ACTIONS` (vendored).
- Produces:
  - `Settings` gains five fields after `audit_output`:
    - `docint_key: str | None`, from `DOCINT_WORKBENCH_KEY`.
    - `docint_tools: frozenset[str]`, from `DOCINT_WORKBENCH_TOOLS`: a comma list, blank entries dropped. Blank means `frozenset({"run_python"})`.
    - `docint_wall_s: int`, from `DOCINT_WORKBENCH_WALL_S`, default 60.
    - `docint_slots: int`, from `DOCINT_WORKBENCH_SLOTS`, default 2.
    - `require_purpose: frozenset[str]`, from `AUDIT_REQUIRE_PURPOSE`, parsed with `parse_required_actions(value, known=ACTIONS - {"lifecycle"})`; default empty.

    `open_webui_key` stays `str | None`, and is now `None` in the api role too when unset.
  - `load_settings(env, *, role)` in the api role:
    - Every key that is set has at least `MIN_KEY_LENGTH` (32) characters once stripped, or a `ConfigError` names it.
    - With neither key set: `ConfigError("OPEN_WEBUI_WORKBENCH_KEY or DOCINT_WORKBENCH_KEY must be set to at least 32 characters")`.
    - While `DOCINT_WORKBENCH_KEY` is set: `5 <= docint_wall_s <= job_wall_s` and `docint_slots < executor_slots`, or a `ConfigError` names the variable.
    - An unknown action in `AUDIT_REQUIRE_PURPOSE` is a `ConfigError` naming that variable.

    The executor role reads none of the five and keeps their defaults, with `docint_key=None`.
  - In `workbench/api/auth.py`:
    - `Door = Literal["terminal", "mcp"]` and `Files = Literal["workspace", "inline"]`.
    - `Caller(name: str, key: str = field(repr=False), tools: frozenset[str], door: Door = "terminal", files: Files = "workspace", wall_s: int | None = None, slots: int | None = None)`.
    - `CallerRegistry(callers)` refuses two callers with the same key: `ConfigError("two callers hold the same key")`, which never names the key. It has `callers -> tuple[Caller, ...]` and `get(name: str) -> Caller` (raises `KeyError`).
    - `CallerRegistry.from_settings(settings)` builds `open-webui` (terminal, workspace, `ALL_TOOLS`, `wall_s=settings.job_wall_s`, `slots=None`) when its key is set, and `docint` (mcp, inline, `docint_tools`, `docint_wall_s`, `docint_slots`) when its key is set. With neither: `ConfigError`.
    - `MCP_PATH: Final = "/mcp"`. `door_allows(caller: Caller, path: str) -> bool`: an mcp caller is allowed `MCP_PATH` alone; a terminal caller every path but that one.
    - `KeyGate`: once the key is checked, a path the caller's door does not allow answers `404 {"detail": errors.NOT_FOUND}`, before any body is read. `OPEN_PATHS` stay open to everyone.
    - `class InvalidPrincipal(ValueError)`, whose message is exactly `"missing or invalid user"` or `"invalid session"`.
    - `principal_from_headers(caller: Caller, headers: Mapping[str, str]) -> Principal`. `require_principal` calls it and answers `400` with the message.

- [ ] **Step 1: Write the failing tests**

In `tests/api/conftest.py`: add `DOCINT_KEY = "d" * 32`, and `HD = {"Authorization": f"Bearer {DOCINT_KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}`. The `settings` fixture also sets `"DOCINT_WORKBENCH_KEY": DOCINT_KEY`.

```python
# tests/test_config.py
D = "d" * 32

def test_either_key_alone_starts_the_api() -> None:
    assert load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api").docint_key is None
    assert load_settings({"DOCINT_WORKBENCH_KEY": D}, role="api").open_webui_key is None

@pytest.mark.parametrize("env", [{}, {"OPEN_WEBUI_WORKBENCH_KEY": "  "}, {"DOCINT_WORKBENCH_KEY": "d" * 31},
                                 {"OPEN_WEBUI_WORKBENCH_KEY": "k" * 31, "DOCINT_WORKBENCH_KEY": D}])
def test_no_usable_key_stops_the_api(env: dict[str, str]) -> None:  # ConfigError; its message holds no key value

def test_the_docint_defaults() -> None:
    s = load_settings({"DOCINT_WORKBENCH_KEY": D}, role="api")
    assert (s.docint_tools, s.docint_wall_s, s.docint_slots, s.require_purpose) == (frozenset({"run_python"}), 60, 2, frozenset())

@pytest.mark.parametrize(("name", "value", "named"), [("DOCINT_WORKBENCH_WALL_S", "121", "DOCINT_WORKBENCH_WALL_S"),
    ("DOCINT_WORKBENCH_WALL_S", "4", "DOCINT_WORKBENCH_WALL_S"), ("DOCINT_WORKBENCH_SLOTS", "4", "DOCINT_WORKBENCH_SLOTS"),
    ("WORKBENCH_EXECUTOR_SLOTS", "2", "DOCINT_WORKBENCH_SLOTS")])
def test_the_docint_rules_stop_the_api_never_the_executor(name: str, value: str, named: str) -> None:
    env = {"DOCINT_WORKBENCH_KEY": D, name: value}
    with pytest.raises(ConfigError, match=named):
        load_settings(env, role="api")
    assert load_settings(env, role="executor").docint_key is None

def test_the_docint_rules_wait_for_its_key() -> None:  # {OPEN_WEBUI key, WORKBENCH_EXECUTOR_SLOTS "2"} loads (api)
def test_the_tool_list_is_a_comma_list() -> None:  # " run_python , ," -> frozenset({"run_python"}); "" -> the default
def test_require_purpose_is_parsed_and_checked() -> None:
    # "query, Disclose,combine" -> frozenset({"query", "disclose", "combine"});
    # "quiery" -> ConfigError match "AUDIT_REQUIRE_PURPOSE"; the executor role -> frozenset()
```

Update `test_every_variable_is_read_by_its_exact_name` for the five new variables and fields, `test_defaults_match_the_spec` for the new fields, and `test_api_requires_a_long_key`, whose rule has changed.

```python
# tests/api/test_callers.py
@pytest.mark.parametrize(("method", "path"), [("GET", "/files/list"), ("POST", "/files/upload"), ("POST", "/tools/run_python"),
    ("GET", "/openapi.json"), ("GET", "/api/config"), ("GET", "/api/v1/terminals/t/mcp"), ("GET", "/nowhere")])
def test_an_mcp_caller_finds_nothing_but_its_door(client: TestClient, method: str, path: str) -> None:
    r = client.request(method, path, headers=HD)
    assert (r.status_code, r.json()) == (404, {"detail": "not found"})

@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_the_service_routes_stay_open_to_every_caller(client, path) -> None:  # 200 with HD

@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_terminal_caller_finds_no_mcp(client, method) -> None:  # /mcp with H -> (404, {"detail": "not found"})

@pytest.mark.parametrize(("key", "path", "passes"), [(DOCINT_KEY, "/files/upload", False), (DOCINT_KEY, "/mcp", True),
                                                     (KEY, "/mcp", False), (KEY, "/files/upload", True)])
def test_the_door_answers_before_any_body_is_read(settings, key, path, passes) -> None:
    # as test_contract.py::test_the_gate_reads_no_body_without_a_key, with a 40 MiB Content-Length:
    # asked == (["app"] if passes else []); when refused, sent[0]["status"] == 404

def test_an_open_webui_user_id_with_docints_key_reaches_nothing(client, store) -> None:
    # put(store, "q.csv") as open-webui user-a; with HD, /files/list, /files/view and /files/display of
    # /workspace/chat-1/q.csv answer 404 {"detail": "not found"}; the file is still listed with H

def test_the_registry_holds_a_row_per_key(settings) -> None:
    # (door, files, tools, wall_s, slots): open-webui ("terminal", "workspace", ALL_TOOLS, 120, None),
    # docint ("mcp", "inline", frozenset({"run_python"}), 60, 2); neither key in repr(registry.callers)
def test_two_callers_with_one_key_stop_the_start(settings) -> None:  # replace(settings, docint_key=KEY) -> ConfigError; KEY not in str(exc)
def test_authentication_compares_every_caller(settings, monkeypatch) -> None:  # counted hmac.compare_digest calls == 2 for either key and for a wrong one

@pytest.mark.parametrize(("headers", "message"), [({}, "missing or invalid user"), ({"X-User-Id": "../x"}, "missing or invalid user"),
                                                  ({"X-User-Id": "user-a", "X-Session-Id": "a/b"}, "invalid session")])
def test_principal_from_headers_refuses_with_the_doors_texts(headers, message) -> None:  # InvalidPrincipal, str(exc) == message
def test_principal_from_headers_names_the_caller_and_defaults_the_chat() -> None:  # == Principal("docint", "user-a", "default")
```

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/test_config.py tests/api/test_callers.py -q`. Expected: FAIL (no `docint_key`, no door check).
- [ ] **Step 3: Implement it** in `workbench/config.py` and `workbench/api/auth.py`. `KeyGate` keeps its single constant-time `authenticate` over all callers, then calls `door_allows`.
- [ ] **Step 4: Run them to verify they pass.** Run `uv run pytest tests/test_config.py tests/api -q`. Expected: all PASS, the existing api tests included.
- [ ] **Step 5: Commit:** `git commit -m "feat(callers): a door, a file form and limits per caller; either key starts the api"`

### Task 2: Registry, errors and a document per caller (W2)

**Files:**
- Create: `workbench/api/registry.py`, `tests/api/test_registry.py`
- Modify:
  - `workbench/api/auth.py`: `ALL_TOOLS` is gone; `require_tool`; the form check.
  - `workbench/api/errors.py`, `workbench/api/openapi.py`.
  - `workbench/api/contract.py`: `/openapi.json`.
  - `workbench/api/main.py`: `app.state.registry`.
  - The nine tool routes, in `workbench/api/tools.py`, `files.py`, `tools_templates.py` and `tools_output.py`.
  - `tests/api/test_contract.py` (`ALL_TOOLS`, `tool_openapi`), `tests/test_docs.py` (`tool_openapi`), `tests/api/test_callers.py`.

**Interfaces:**
- Consumes: `Caller`, `CallerRegistry` (Task 1).
- Produces:
  - In `workbench/api/registry.py`, which imports `Caller` only under `TYPE_CHECKING` because `auth.py` imports this module:
    - `WORKSPACE_TOOLS: Final`: the nine names, formerly `auth.ALL_TOOLS`. `INLINE_TOOLS: Final = frozenset({"run_python"})`. `FORM_TOOLS: Final = {"workspace": WORKSPACE_TOOLS, "inline": INLINE_TOOLS}`.
    - `@dataclass(frozen=True) class ToolEnv`, with fields `p: Principal`, `caller: Caller`, `settings: Settings`, `store: Store`, `audit: AuditLog`, `jobs: JobQueue | None`, `purpose: Purpose | None` and `request_id: str | None`.
    - `@dataclass(frozen=True) class ToolOutcome`, with fields `structured: dict[str, Any]` and `blocks: list[dict[str, Any]]`: a tool's answer before the door wraps it.
    - `class PurposeRequired(Exception)`: a required purpose was not stated, and the `denied` event is already written.
    - `@dataclass(frozen=True) class ToolSpec`, with the fields:
      - `name: str`, `title: str`, `description: str`
      - `input_model: type[BaseModel] | None`, `output_model: type[BaseModel] | None`
      - `form: Literal["workspace", "inline"]`
      - `run: Callable[[Any, ToolEnv], Awaitable[ToolOutcome]] | None = None`
      - `input_schema: Callable[[Caller], dict[str, Any]] | None = None`

      A workspace spec has `run` and `input_schema` `None`: it is not served on MCP in version 2.
    - `workspace_specs(app: FastAPI) -> tuple[ToolSpec, ...]`, built from the routes tagged `TOOL_TAG`: `operation_id`, `summary`, `description`, the body model or `None`, the `response_model` or `None`.
    - `class Registry(specs: Iterable[ToolSpec])`, with `for_caller(caller) -> list[ToolSpec]` (form `caller.files`, name in `caller.tools`, in name order) and `find(caller, name) -> ToolSpec | None` (under the same rule).
  - `auth.CallerRegistry.from_settings`: a caller's tools must lie within `FORM_TOOLS[caller.files]`, or a `ConfigError` names the caller's variable (`DOCINT_WORKBENCH_TOOLS` for docint).
  - `auth.require_tool(name: str) -> Callable[..., Awaitable[Caller]]`: a dependency on `require_caller` that answers `404 {"detail": "not found"}` when `name` is not on the caller's list. Each of the nine routes gets `dependencies=[Depends(require_tool("<operation_id>"))]`. No route gains an OpenAPI parameter from it.
  - `errors.describe(exc: Exception) -> tuple[int, str]`: the status and short text `_answer` sends for `_ANSWERS`, and `(500, "storage error")` for any other `StoreError`. An `HTTPException` with a string `detail` gives `(status_code, detail)`. Anything else gives `(500, INTERNAL)`.
  - `errors.compact(errors: Sequence[Mapping[str, Any]]) -> dict[str, object]` returns `{"detail": [...]}`, with `more_errors` only past `MAX_ERRORS`: the shape `_invalid` sends today. `_answer` and `_invalid` become one-liners over these two.
  - `openapi.tool_openapi(app: FastAPI, caller: Caller) -> dict[str, Any]`: the routes tagged `TOOL_TAG` whose `operation_id` is on `caller.tools`, cached per caller name in `app.state.tool_openapi` (a dict). `/openapi.json` passes in the caller it authenticated.
  - Once the routers are in, `create_app` sets `app.state.registry = Registry(workspace_specs(app))`. Task 5 adds the inline specs.

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_registry.py
def test_the_registry_equals_the_tool_routes(app: FastAPI) -> None:
    specs = workspace_specs(app)
    assert len(specs) == 9 and {s.name for s in specs} == WORKSPACE_TOOLS
    assert all(s.form == "workspace" and s.run is None and s.title and s.description for s in specs)

def test_a_caller_sees_the_tools_of_its_form_in_name_order() -> None:
    # Registry of synthetic specs "b", "a" (workspace) and "c" (inline): a workspace caller listing {a, b, c} gets [a, b];
    # an inline caller listing {c} gets [c]; find(inline caller, "a") is None

@pytest.mark.parametrize("tools", [frozenset({"list_files"}), frozenset({"run_python", "nowhere"})])
def test_a_tool_without_the_callers_form_stops_the_start(settings, tools) -> None:  # ConfigError match "DOCINT_WORKBENCH_TOOLS"

@pytest.mark.parametrize(("exc", "answer"), [
    (PathError("invalid file name"), (400, "invalid file name")), (NotFound("x"), (404, "not found")),
    (Conflict("already exists"), (409, "already exists")), (QuotaExceeded("quota exceeded"), (507, "quota exceeded")),
    (AuditWriteError("x"), (500, "audit unavailable")), (Busy("queue_full"), (503, "busy, retry later")),
    (RequestTooLarge("x"), (422, "request too large")), (StoreError("x"), (500, "storage error")),
    (HTTPException(422, "the code could not be completed: time"), (422, "the code could not be completed: time")),
    (RuntimeError("/data/workspaces/user-a"), (500, "internal error")),
])
def test_describe_is_what_the_handlers_answer(exc: Exception, answer: tuple[int, str]) -> None:
    assert describe(exc) == answer

def test_compact_keeps_where_and_why_never_the_value() -> None:
    # 25 errors carrying "input" and "ctx", one loc part of 400 characters: 20 listed, more_errors == 5,
    # no "input" or "ctx" key, the long part ends with " [cut]"
```

Appended to `tests/api/test_callers.py`:

```python
def test_a_reduced_list_is_refused_by_require_tool(settings, store, audit, monkeypatch) -> None:
    # CallerRegistry.from_settings patched to give open-webui WORKSPACE_TOOLS - {"read_document"}:
    # POST /tools/read_document -> (404, {"detail": "not found"}); /openapi.json lacks read_document and lists the other
    # eight; POST /tools/list_files -> 200

def test_each_caller_gets_its_own_document(app) -> None:
    # tool_openapi(app, open-webui) has the nine operationIds; tool_openapi(app, docint) has no path;
    # a second call returns the same object for each
```

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/api/test_registry.py tests/api/test_callers.py -q`. Expected: FAIL (no `workbench.api.registry`).
- [ ] **Step 3: Implement it.** Move `ALL_TOOLS` to `registry.WORKSPACE_TOOLS` and update the importers of `ALL_TOOLS` and the callers of `tool_openapi` (`tests/api/test_contract.py`, `tests/test_docs.py`).
- [ ] **Step 4: Run them to verify they pass.** Run `make test`. Expected: all PASS. Open WebUI's document still has nine operations, and none of them has a header parameter.
- [ ] **Step 5: Commit:** `git commit -m "feat(registry): one tool registry, a document per caller, require_tool on the nine routes"`

### Task 3: Protocol and supervisor (W3)

**Files:**
- Modify: `workbench/protocol.py`, `workbench/executor/supervisor.py`, `tests/executor/test_protocol.py`, `tests/executor/test_supervisor.py`

**Interfaces:**
- Produces:
  - `MAX_INLINE_FILES: Final = 8` and `MAX_INLINE_BYTES: Final = 512 * 1024`.
  - `class InlineFile(_Message)`, with fields `name: str` and `content: str`. `name` must pass `check_filename` and must not be `out`.
  - `JobRequest.inline_inputs: list[InlineFile] = Field(default_factory=list, max_length=MAX_INLINE_FILES)`. A model validator refuses a repeated name, contents over `MAX_INLINE_BYTES` together in UTF-8, and `inline_inputs` given together with `input_files`.
  - `Supervisor._write_inline(self, req: JobRequest, target: int) -> None` writes each entry into the opened working folder with `CREATE_FLAGS`, root's and `0444` (`settle`), as `_copy_in` writes a copy. `_set_up` calls it right after `_copy_inputs`. `spec.json` does not change.

- [ ] **Step 1: Write the failing tests**

```python
# tests/executor/test_protocol.py
def test_a_request_has_no_inline_inputs_by_default() -> None:  # JobRequest(**_fields()).inline_inputs == []
def test_a_request_with_inline_inputs_round_trips() -> None:  # decode_request(encode(req)) == req
@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "out", "x\x00y"])
def test_an_inline_name_is_one_plain_file_name_and_not_out(name: str) -> None:  # ValidationError
def test_inline_names_are_unique() -> None:
def test_at_most_eight_inline_inputs() -> None:  # 8 pass, 9 are refused
def test_inline_texts_together_stay_within_512_kib() -> None:  # "é" * (256 * 1024) passes; one more byte in a second entry is refused
def test_inline_inputs_never_come_with_input_files() -> None:
```

```python
# tests/executor/test_supervisor.py: unit (use_slots=False)
async def test_inline_inputs_are_written_into_the_working_folder_read_only(volume, monkeypatch) -> None:
    # the runner reports sorted(os.listdir(".")) == ["out", "passages.json"], the content as sent, mode "0o444", owner ME
async def test_inline_data_never_reaches_the_spec(volume, monkeypatch) -> None:
    # spec.json's keys stay {"tool", "args", "chat_id", "max_chars", "file_mb"}; a marker of the data is not in its text
async def test_an_inline_job_opens_no_workspace(volume, monkeypatch) -> None:  # caller "docint": ok; no workspaces/docint before or after
@pytest.mark.parametrize("ending", ["ok", "failing process", "wall timeout"])
async def test_inline_inputs_go_with_the_job_folder(volume, monkeypatch, ending) -> None:  # _entries(volume / "jobs") == []
```

```python
# tests/executor/test_supervisor.py: @pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_a_slot_inline_inputs_are_roots_and_unchangeable(root_volume, monkeypatch) -> None:
    # owner("passages.json") == [0, 0, "0o444"]; appending, os.chmod, os.unlink and os.rename are "denied"; reading is "ok"
async def test_as_root_one_jobs_inline_inputs_are_unreadable_to_another(root_volume, monkeypatch) -> None:
    # _two_jobs / _two_job_runner / _crossed: each job opening the other's jobs/<id>/workspace/<chat>/passages.json gets PermissionError
async def test_as_root_inline_inputs_are_gone_after_the_job(root_volume, monkeypatch) -> None:  # _entries(root_volume / "jobs") == []
```

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/executor/test_protocol.py tests/executor/test_supervisor.py -q`. Expected: FAIL (`inline_inputs` is an unknown field).
- [ ] **Step 3: Implement** `InlineFile`, the validator and `_write_inline`.
- [ ] **Step 4: Verify.** Run `make test`, then `make test-executor-root`. Expected: PASS in both `/dev/shm` shapes, the root tests included.
- [ ] **Step 5: Commit:** `git commit -m "feat(executor): inline inputs, root's and 0444 in the job folder, gone with it"`

### Task 4: The queue (W4)

**Files:**
- Modify: `workbench/api/jobs.py`, `tests/api/test_jobs.py`

**Interfaces:**
- Consumes: `InlineFile` (Task 3).
- Produces (all in `workbench/api/jobs.py`):
  - `MCP_QUEUE_WAIT_S: Final = 5`.
  - `RETURN_TYPES: Final = {".png": "image/png", ".csv": "text/csv", ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json"}`; the suffix is matched lower-cased.
  - `MAX_RETURNED: Final = 8`, `IMAGE_MAX: Final = 512 * 1024`, `TEXT_MAX: Final = 256 * 1024`, `RETURN_MAX: Final = 1024 * 1024`, `PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"`.
  - `@dataclass(frozen=True) class CallLimits`, with fields `wall_s: int`, `wait_s: int` and `slots: int | None = None`.
  - `@dataclass(frozen=True) class TakenFile`, with fields `name: str`, `mime: str` and `data: bytes`.
  - `SkipReason = Literal["type", "signature", "encoding", "count", "size", "total", "unreadable"]`.
  - `@dataclass(frozen=True) class SkippedOutput`, with fields `name: str`, `size: int` and `reason: SkipReason`.
  - `@dataclass(frozen=True) class TakenOutputs`, with fields `files: tuple[TakenFile, ...] = ()` and `skipped: tuple[SkippedOutput, ...] = ()`.
  - `class Taker(Protocol)`, with one method `take(self, result: JobResult, taken: TakenOutputs) -> None`. It runs in a worker thread and records the job; when it raises, `submit_inline` raises that and nothing goes out.
  - `JobQueue.submit(p, tool, args, input_files, keeper=None, *, call: CallLimits | None = None)`. Without `call` it behaves as in v1: `job_wall_s`, `queue_wait_s`, no caller cap.
  - `JobQueue.submit_inline(self, p: Principal, tool: str, args: dict[str, Any], inline_inputs: Sequence[InlineFile], taker: Taker, call: CallLimits) -> tuple[JobRecord, JobResult, TakenOutputs]`.
- Rules:
  - **`_CallerGate(slots, waiting_max)`.** One per caller name, made on its first capped call. `running` is an `asyncio.Semaphore(slots)`; `waiting_max = slots * max(1, queue_max // executor_slots)`.
  - **Admission.** `_admit(gate, caller_gate, wait_s)` counts a call as waiting when the user's turn, the caller's gate or the global slot is taken. It refuses `queue_full` at once past `queue_max`, past the user's share or past the caller's share. It acquires the user's turn, then the caller's gate, then the global slot, all within `wait_s`; past that it refuses `wait_timeout`. On failure it releases in reverse order.
  - **Limits.** The job gets `Limits(wall_s=w, cpu_s=w, ...)` with `w = min(call.wall_s, settings.job_wall_s)`, and `_execute` waits `w + _READ_GRACE_S`.
  - **`_take(job_id: str, result: JobResult) -> TakenOutputs`.** It runs in a worker thread and reads the outputs of an `ok` result only. For each output:
    - The path check of `_register` applies: the staged path must be `staging/<job-id>/<check_filename(name)>`.
    - It is opened with `O_NOFOLLOW`. Only a regular file is read, and at most its cap plus one byte.
    - Steps, in order:
      1. A type outside `RETURN_TYPES` is skipped as `type` before any cap applies.
      2. The files are sorted: `.png`, then `.csv`, then the rest, each group by name.
      3. Past `MAX_RETURNED` kept files, a file is skipped as `count`.
      4. Past its own cap, it is skipped as `size`.
      5. A PNG without the signature is skipped as `signature`, and a text that is not strict UTF-8 as `encoding`.
      6. Past `RETURN_MAX` in all, a file is skipped as `total`, and the next file is still tried.
      7. A path, open or read failure is skipped as `unreadable`.
  - **The taker.** `_take` and then `taker.take` run as one thread call, under `outlast` as `_keep` runs. They are called for every result the executor answered, whatever its status. `_settle` removes the staging folder whatever happened. Nothing is registered: no `files` row, and no `workspaces/<caller>/` folder.

- [ ] **Step 1: Write the failing tests** (`FakeClient` and `until` as the file has them; `scaled` from `tests/conftest.py`)

```python
DOCINT = CallLimits(wall_s=60, wait_s=5, slots=2)

def docint_user(n: int) -> Principal:
    return Principal("docint", f"{n:064x}", "default")

class Takes:  # a Taker; remembers (result.status, taken) and the staging folder's names when it ran; raises `fail` if set

def test_mcp_callers_wait_five_seconds() -> None:
    assert MCP_QUEUE_WAIT_S == 5

async def test_a_capped_caller_runs_at_most_its_slots_and_leaves_room_for_others(...) -> None:
    # 4 held docint users: executor.running reaches 2 and stays; an open-webui user's submit completes meanwhile
async def test_the_callers_waiting_calls_are_capped_across_many_users(...) -> None:
    # 2 running, 8 more distinct users waiting; the 11th distinct user -> Busy("queue_full") within 0.5 s;
    # an open-webui user still queues and runs once the gate opens
async def test_a_per_call_wait_replaces_the_queue_wait(...) -> None:  # queue_wait_s 60, a held slot, wait_s=1 -> Busy("wait_timeout") within scaled(3) s
@pytest.mark.parametrize(("wall", "job_wall", "expected"), [(30, 120, 30), (200, 120, 120)])
async def test_the_wall_is_the_least_of_the_call_and_the_setting(...) -> None:
    # req.limits.wall_s == req.limits.cpu_s == expected; executor.timeouts[-1] == expected + 300
async def test_inline_inputs_reach_the_request_and_nothing_the_workspace(...) -> None:
    # req.inline_inputs as given; req.input_files == []; no data_dir / "workspaces" / "docint"; store.files_in_chat(p) == []
async def test_outputs_come_back_png_then_csv_then_the_rest_by_name(...) -> None:
    # staged {"z.txt", "b.csv", "a.png", "c.json", "n.md", "x.xlsx", "y.html"} (a.png with the signature)
    # -> [f.name for f in taken.files] == ["a.png", "b.csv", "c.json", "n.md", "z.txt"];
    #    {(s.name, s.reason) for s in taken.skipped} == {("x.xlsx", "type"), ("y.html", "type")}; mimes from RETURN_TYPES
async def test_upper_case_suffixes_come_back(...) -> None:  # "Chart.PNG" -> TakenFile mime "image/png"
async def test_a_png_needs_its_signature_and_text_must_be_strict_utf8(...) -> None:
    # "fake.png" b"GIF89a..." -> "signature"; "t.csv" "Müller\n".encode("latin-1") -> "encoding"; "bom.csv" b"\xef\xbb\xbfa,b\n" comes back
async def test_the_return_caps(...) -> None:
    # 9 small csv -> 8 back, "i.csv" (last by name) "count"; a PNG of IMAGE_MAX + 1 -> "size"; a txt of TEXT_MAX + 1 -> "size";
    # two PNGs of IMAGE_MAX each and "c.csv" -> both PNGs back (1 MiB exactly), "c.csv" "total"
async def test_only_the_jobs_own_staged_regular_files_are_read(...) -> None:
    # a staged_path outside staging/<job-id>/, a symlink in a staged file's place and a folder -> "unreadable"
async def test_the_taker_records_before_anything_returns_and_its_failure_sends_nothing(...) -> None:
    # Takes(fail=AuditWriteError("x")) -> submit_inline raises AuditWriteError; the taker saw the staged names; the staging folder is gone
@pytest.mark.parametrize("ending", ["ok", "error", "cancelled"])
async def test_staging_is_removed_on_success_error_and_cancellation(...) -> None:
    # "cancelled": the task is cancelled while the executor holds; no staging/<job-id>; the job row's status is the ending
async def test_a_failed_job_takes_no_output_but_is_still_handed_to_the_taker(...) -> None:  # status "timeout" -> TakenOutputs(); taker called once
async def test_open_webui_is_never_capped(...) -> None:  # with docint gates in use, 4 open-webui users run at once (executor.peak == 4)
```

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/api/test_jobs.py -q`. Expected: FAIL (no `CallLimits`).
- [ ] **Step 3: Implement it** in `workbench/api/jobs.py`. The existing `submit` tests stay as they are.
- [ ] **Step 4: Run them to verify they pass.** Run `uv run pytest tests/api/test_jobs.py -q`, then `make test`. Expected: all PASS.
- [ ] **Step 5: Commit:** `git commit -m "feat(queue): inline jobs, outputs taken back by type and cap, a running and a waiting cap per caller"`

### Task 5: Inline `run_python` and its audit (W5, W6)

**Files:**
- Create: `workbench/api/tools_inline.py`, `tests/api/test_tools_inline.py`
- Modify:
  - `workbench/api/tools.py`: the shared helper.
  - `workbench/api/audit.py`.
  - `workbench/api/main.py`: `config.audit`, and the `Registry` gets `INLINE_SPECS`.
  - `tests/api/test_audit.py`.
  - `docs/audit.md`: only the event-key row that `tests/test_docs.py` requires, `tool.run_python.inline`. Task 7 writes the prose.

**Interfaces:**
- Consumes: `ToolSpec`, `ToolEnv`, `ToolOutcome`, `PurposeRequired`, `INLINE_TOOLS` (Task 2); `submit_inline`, `CallLimits`, `TakenOutputs`, `RETURN_TYPES`, `MCP_QUEUE_WAIT_S` (Task 4).
- Produces, in `workbench/api/tools.py`:
  - `class NotCompleted(HTTPException)`: status 422, `detail` `"the code could not be completed: <reason>"`, raised where `_python_payload` raises that 422 today. The terminal answer does not change.
  - `class CodeRun(BaseModel)`, formerly `_CodeRun`.
  - `python_args(code: str, wall_s: int) -> dict[str, Any]`, which returns `{"code": code, "budget_s": max(1, wall_s * 9 // 10)}`.
  - `read_code_run(result: JobResult) -> tuple[CodeRun, str | None]`: `_python_payload`, then `_parse(CodeRun, ...)`. It raises `NotCompleted`, or another `HTTPException`.
  - `fit_python[M: BaseModel](answer: M, limit_kb: int) -> M`, formerly `_fit_python`, for any model with `stdout`, `stderr` and `truncated`.

  The terminal `run_python` is rebuilt on these four; its tests pin that its behaviour is the same.
- Produces, in `workbench/api/tools_inline.py`:
  - `DATA_NAME: Final = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.(json|csv|txt|md)$"`, `MAX_DATA: Final = 8`, `MAX_DATA_BYTES: Final = 512 * 1024`, `MIN_TIME_LIMIT_S: Final = 5`.
  - `class InlineData(BaseModel)`, `extra="forbid"`: `name: str` (matching `DATA_NAME`) and `content: str`.
  - `class InlinePythonRequest(BaseModel)`, `extra="forbid"`, validated with `context={"wall_cap": int}`:
    - `code: str`, from 1 to `MAX_CODE` characters.
    - `data: list[InlineData]`, at most `MAX_DATA` entries, unique names, at most `MAX_DATA_BYTES` of UTF-8 together.
    - `time_limit_s: int | None`, at least 5 and at most the context's `wall_cap`.
  - `input_schema(caller: Caller) -> dict[str, Any]`: the request's schema, in which `properties.time_limit_s` is `{"type": "integer", "minimum": 5, "maximum": caller.wall_s, "description": ...}` with no `anyOf`. `required == ["code"]`, and `additionalProperties` is `false`.
  - `class OutputInfo(BaseModel)`, with fields `name: str`, `mime: str`, `bytes: int` and `returned: bool`.
  - `class InlinePythonResult(BaseModel)`, with fields `stdout`, `stderr`, `truncated`, `job_id`, `files: list[OutputInfo]`, `hint: str | None` and `reason: None = None`. It is serialised with the `None`s kept, as in spec §3.4's example.
  - `output_uri(job_id: str, name: str) -> str` returns `f"workbench://jobs/{job_id}/out/{urllib.parse.quote(name, safe='')}"`.
  - `@dataclass(frozen=True) class InlineTaker`, a `Taker`, with fields `audit`, `p`, `purpose`, `request_id`, `count` and `input_bytes`. `take()` writes `run_python_inline`:
    - `target_kind="inline_data"` and `count`.
    - Criteria `{"job": result.job_id, "input_bytes": ..., "outputs": len(taken.files), "dropped": len(taken.skipped)}`.
    - When the answer will be an `isError` result, also `"refused"`: `"not_completed"` if `read_code_run` raises `NotCompleted`, `"refused"` for any other `HTTPException` it raises.
    - Outcome `ok` for an `ok` job whose answer reads, else `error`.
  - `async def run_python_inline(body: InlinePythonRequest, env: ToolEnv) -> ToolOutcome`, in this order:
    1. If `"combine"` is in `env.settings.require_purpose` and no purpose was stated, write a `denied` event with no target and the criteria `{"refused": "purpose_required", "container": f"chat:{p.chat_id}"}`, then raise `PurposeRequired`. No job starts.
    2. `cap = caller.wall_s or settings.job_wall_s`, then `wall = min(body.time_limit_s or cap, cap, settings.job_wall_s)`.
    3. `env.jobs.submit_inline(p, "run_python", python_args(body.code, wall), [InlineFile(...)], taker, CallLimits(wall, MCP_QUEUE_WAIT_S, caller.slots))`. With `env.jobs` `None`, raise `Busy("executor_busy")`.
    4. Read the job's answer with `read_code_run(result)`.
    5. Build the `InlinePythonResult` and fit it with `fit_python(..., settings.result_max_kb)`. `files` has one entry per output: the returned ones in return order, then the skipped ones in name order. `mime` comes from `RETURN_TYPES` by the lower-cased suffix, else `application/octet-stream`.
    6. Add one block per returned file, in return order:
       - A PNG: `{"type": "image", "data": <base64>, "mimeType": "image/png", "annotations": {"audience": ["user"]}}`.
       - A text: `{"type": "resource", "resource": {"uri": output_uri(...), "mimeType": mime, "text": text}}`.
  - The hint: one sentence per skip reason that occurred, each naming its count, joined with the runner's hint and `read_code_run`'s note (`tools._join`, capped at `MAX_HINT`). The phrases the tests pin:

    | Skip reason | Phrase |
    |---|---|
    | `type` | "only .png, .csv, .txt, .md and .json come back" |
    | `size`, a PNG | "save it smaller, for example plt.savefig(..., dpi=100)" |
    | `size`, a text | "a text comes back up to 256 KiB" |
    | `count` | "at most 8 files come back" |
    | `total` | "1 MiB in all" |
    | `encoding` | "write text files as UTF-8" |
    | `signature` | "is not a PNG" |
    | `unreadable` | "could not be read" |
  - `RUN_PYTHON: Final = ToolSpec(name="run_python", title="Run Python on inline data", ..., form="inline", run=run_python_inline, input_schema=input_schema)`. Its description tells the model three things: the data files are in the current folder, read-only; tables go into `out/` as CSV and charts as PNG, and the types and caps that come back; there is no network and nothing can be installed. `INLINE_SPECS: Final = (RUN_PYTHON,)`.
- Produces, in `workbench/api/audit.py`:
  - `OPERATIONS["run_python_inline"] = ("combine", "tool.run_python.inline")`.
  - `AuditLog.record(..., target_kind: str | None = "file", count: int | None = None, request_id: str | None = None)`. `target_kind=None` writes no target; `count` replaces `len(file_ids)`; `request_id` reaches the event and every continuation, so `_emit` passes it on.
- Produces, in `workbench/api/main.py`:
  - `_audit_config(settings, callers: CallerRegistry)` adds `"callers"` and `"require_purpose": sorted(settings.require_purpose)`. `callers` holds one entry per caller in name order: `{"name", "door", "tools": sorted list, "wall_s", "slots"}`.
  - `create_app` builds `Registry([*workspace_specs(app), *INLINE_SPECS])`.

- [ ] **Step 1: Write the failing tests**

The fixtures: the `app` and `fake_executor` of `tests/api/test_tools_python.py`, imported. `env(client, p=Principal("docint", "a" * 64, "default"), purpose=None, request_id=None) -> ToolEnv` builds the environment from `client.app.state`, with the caller `callers.get("docint")`. Every call runs on the app's loop, as `client.portal.call(run_python_inline, body, env)`. `body(**fields)` is `InlinePythonRequest.model_validate(fields, context={"wall_cap": 60})`.

```python
PASSAGES = {"name": "passages.json", "content": '[{"n": 1, "text": "synthetic harbour notes", "author": "user-a"}]'}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
COUNTS = b"author,posts\nuser-a,12\nuser-b,7\n"

# §3.4, the request
@pytest.mark.parametrize("code", ["", "x" * 50_001])
def test_code_is_1_to_50000_characters(code: str) -> None:  # refused; "x" * 50_000 passes
@pytest.mark.parametrize("name", ["passages.json", "a.csv", "B-1.txt", "x_y.md", "a" + "b" * 99 + ".json"])
def test_these_data_names_pass(name: str) -> None:
@pytest.mark.parametrize("name", [".a.json", "-a.csv", "a.xlsx", "a.JSON", "a/b.csv", "out", "a" + "b" * 100 + ".json", "ü.csv", "a.csv\n"])
def test_these_data_names_are_refused(name: str) -> None:
def test_data_names_are_unique() -> None:
def test_at_most_eight_entries() -> None:
def test_all_data_together_is_at_most_512_kib_in_utf8() -> None:  # "ü" * (256 * 1024) passes; one byte more is refused
@pytest.mark.parametrize("limit", [4, 61, 0, -1])
def test_time_limit_outside_5_to_the_cap_is_refused_never_clamped(limit: int) -> None:  # 5, 60 and absent pass
def test_unknown_fields_are_refused() -> None:  # "timeout" at the top level; "mime" in an entry
def test_the_schema_gives_time_limit_the_callers_range(settings) -> None:
    # input_schema(docint)["properties"]["time_limit_s"] has type "integer", minimum 5, maximum 60;
    # "time_limit_s" not in required; with wall_s 30 the maximum is 30

# §3.4, the job
def test_the_data_goes_inline_and_no_file_goes_in(client, fake_executor, settings) -> None:
    # req.inline_inputs == [InlineFile(**PASSAGES)], req.input_files == [], req.caller == "docint";
    # no settings.data_dir / "workspaces" / "docint"; nothing in the store
@pytest.mark.parametrize(("limit", "job_wall", "wall"), [(None, 120, 60), (30, 120, 30), (None, 40, 40)])
def test_the_wall_is_the_least_of_the_limit_the_cap_and_the_setting(...) -> None:  # req.limits.wall_s == wall; req.args["budget_s"] == wall * 9 // 10
def test_the_call_waits_five_seconds_and_holds_the_callers_slots(...) -> None:  # submit_inline spied: call == CallLimits(60, 5, 2)

# §3.4, the result
def test_the_result_has_the_shape_of_the_spec(client, fake_executor) -> None:
    # payload ran("2 authors"); staged {"chart.png": PNG, "counts.csv": COUNTS}
    # structured keys == {"stdout", "stderr", "truncated", "job_id", "files", "hint", "reason"}; reason is None; job_id 32 hex;
    # files == [{"name": "chart.png", "mime": "image/png", "bytes": len(PNG), "returned": True}, {"name": "counts.csv", "mime": "text/csv", ...}];
    # blocks == [{"type": "image", "data": b64encode(PNG).decode(), "mimeType": "image/png", "annotations": {"audience": ["user"]}},
    #            {"type": "resource", "resource": {"uri": f"workbench://jobs/{job_id}/out/counts.csv", "mimeType": "text/csv", "text": COUNTS.decode()}}]
def test_files_lists_what_did_not_come_back_too(...) -> None:  # "x.xlsx" -> {"returned": False, "mime": "application/octet-stream"}
@pytest.mark.parametrize(("staged", "phrase"), [({"x.xlsx": b"PK"}, "only .png, .csv, .txt, .md and .json come back"),
    ({"big.png": PNG + b"\x00" * (512 * 1024)}, "plt.savefig(..., dpi=100)"), ({"t.csv": "Müller".encode("latin-1")}, "UTF-8")])
def test_the_hint_says_why_a_file_did_not_come_back(...) -> None:
def test_a_uri_holds_the_name_percent_encoded(...) -> None:
    # "Übersicht März #1.csv" -> uri ends "/out/%C3%9Cbersicht%20M%C3%A4rz%20%231.csv"; files[0]["name"] is the name as written
def test_stdout_and_stderr_stay_within_the_result_limit(...) -> None:  # ran("x" * 100_000) -> truncated True; len(json.dumps(structured).encode()) <= 16 * 1024
def test_code_that_could_not_complete_raises_not_completed(...) -> None:  # status "timeout" -> NotCompleted; its event's criteria["refused"] == "not_completed"
def test_a_runner_refusal_raises_its_http_exception(...) -> None:  # error "storage_full: ..." -> HTTPException 507, not NotCompleted; refused "refused"
def test_escape_heavy_data_is_too_large_never_a_500(settings, store, audit, tmp_path) -> None:
    # a JobQueue over ExecutorClient(tmp_path / "none.sock"); 8 entries of 64 KiB of '"' each -> RequestTooLarge;
    # no tool.run_python.inline event; no folder under staging/
def test_nothing_is_registered_and_no_workspace_is_made(...) -> None:
def test_open_webuis_run_python_is_unchanged(client) -> None:
    # tool_openapi(app, open-webui): run_python's request body properties == {"code"}; the route still records tool.run_python
def test_the_inline_specs_are_the_inline_tools() -> None:  # {s.name for s in INLINE_SPECS} == INLINE_TOOLS; every form "inline"

# §3.6, the audit
def test_the_call_is_recorded_as_combine_on_inline_data(...) -> None:
    # purpose Purpose(reason="docint chat"), request_id "req-1": action "combine", operation "tool.run_python.inline",
    # actor {"user": "a" * 64, "via": "service"}, target == {"kind": "inline_data", "count": 1, "container": "chat:default"},
    # criteria == {"caller": "docint", "job": job_id, "input_bytes": len(PASSAGES["content"].encode()), "outputs": 2, "dropped": 0},
    # purpose == {"reason": "docint chat"}, request_id == "req-1"
def test_the_container_is_the_session(...) -> None:  # session "b" * 64 -> "chat:" + "b" * 64
def test_never_recorded(...) -> None:  # markers in the data, a data name, the code, stdout and a refusal's text appear in no audit line
def test_nothing_goes_out_when_the_event_cannot_be_written(broken_audit, ...) -> None:  # AuditWriteError raised; no staging/<job-id>
def test_a_missing_required_purpose_is_denied_without_a_target_and_runs_nothing(...) -> None:
    # require_purpose {"combine"}, no purpose: PurposeRequired; last event outcome "denied", no "target",
    # criteria == {"caller": "docint", "refused": "purpose_required", "container": "chat:default"}; fake_executor.requests == []
def test_a_stated_purpose_meets_the_requirement(...) -> None:
def test_the_terminal_door_keeps_version_1s_rule(...) -> None:  # require_purpose {"combine"}: POST /tools/run_python with H and no purpose -> 200
def test_config_audit_lists_the_callers_and_the_purpose_rule(...) -> None:
    # criteria["callers"] == [{"name": "docint", "door": "mcp", "tools": ["run_python"], "wall_s": 60, "slots": 2},
    #   {"name": "open-webui", "door": "terminal", "tools": sorted(WORKSPACE_TOOLS), "wall_s": 120, "slots": None}];
    # criteria["require_purpose"] sorted; neither key's value in any line
```

```python
# tests/api/test_audit.py
def test_record_takes_a_target_kind_a_count_and_a_request_id(log, stream, alice) -> None:
    log.record("run_python_inline", alice, target_kind="inline_data", count=3, request_id="req-1")
    # last target == {"kind": "inline_data", "count": 3, "container": "chat:chat-1"}; last["request_id"] == "req-1"
def test_a_denial_has_no_target(log, stream, alice) -> None:  # target_kind=None, outcome="denied" -> "target" not in the last line
def test_a_continuation_keeps_the_request_id(log, stream, alice) -> None:  # 201 ids -> both lines carry "req-1"
```

Update `test_every_tool_key_maps_to_an_audit_action` for the new key.

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/api/test_tools_inline.py tests/api/test_audit.py -q`. Expected: FAIL (no `workbench.api.tools_inline`).
- [ ] **Step 3: Implement it.** Refactor `tools.py` first, then run `uv run pytest tests/api/test_tools_python.py -q` and expect PASS unchanged. Then write `tools_inline.py`, the changes to `audit.py` and `main.py`, and the row in `docs/audit.md`.
- [ ] **Step 4: Run them to verify they pass.** Run `make test`. Expected: all PASS, `tests/test_docs.py` included.
- [ ] **Step 5: Commit:** `git commit -m "feat(tools): inline run_python, outputs returned and never kept, recorded as combine on inline data"`

### Task 6: The MCP door (W7)

**Files:**
- Create: `workbench/api/mcp_wire.py`, `workbench/api/mcp.py`, `tests/api/test_mcp.py`, `tests/api/test_mcp_sdk_client.py`
- Modify:
  - `workbench/api/main.py`: the router.
  - `workbench/api/body_limit.py`.
  - `pyproject.toml` and `uv.lock`: `mcp` in `dev`.
  - `tests/test_compose_defaults.py`.

**Interfaces:**
- Consumes: `Registry`, `ToolEnv`, `PurposeRequired` (Task 2); `principal_from_headers`, `InvalidPrincipal`, `MCP_PATH` (Task 1); `describe`, `compact`; `NotCompleted` (Task 5); `Busy`, `RequestTooLarge`; `parse_purpose_header`, `PurposeError`, `clean_request_id` (vendored).
- Produces, in `workbench/api/mcp_wire.py` (no I/O):
  - **Revisions:** `MODERN: Final = "2026-07-28"`, `LEGACY: Final = ("2025-11-25", "2025-06-18")`, `SUPPORTED: Final = (MODERN, *LEGACY)`, `LEGACY_DEFAULT: Final = "2025-11-25"`.
  - **`_meta` keys:** `META: Final = "io.modelcontextprotocol/"`; the keys are `META + "protocolVersion"`, `"clientCapabilities"`, `"clientInfo"` and `"serverInfo"`. If the SDK test shows other names for the client's keys, follow the SDK and say so in the PR.
  - **Codes:** `PARSE_ERROR = -32700`, `INVALID_REQUEST = -32600`, `METHOD_NOT_FOUND = -32601`, `INVALID_PARAMS = -32602`, `INTERNAL_ERROR = -32603`, `HEADER_MISMATCH = -32020`, `UNSUPPORTED_VERSION = -32022`.
  - **Types:**
    - `RequestId = str | int`.
    - `@dataclass(frozen=True) class WireError(Exception)`, with fields `status: int`, `code: int`, `message: str`, `data: Mapping[str, object] | None = None` and `id: RequestId | None = None`.
    - `@dataclass(frozen=True) class Message`, with fields `id: RequestId | None`, `method: str` and `params: dict[str, Any]`. A `None` id means a notification.
    - `@dataclass(frozen=True) class Era`, with fields `version: str` and `modern: bool`.
  - **`parse_message(body: Any) -> Message`.** A list is a batch: `400`, `-32600`. So are a non-object, a `jsonrpc` other than `"2.0"`, a non-string `method`, non-object `params`, and an `id` that is present and not a string or an integer: `null`, a boolean, a float or a container.
  - **`check_media(content_type: str | None, accept: str | None) -> None`.** It raises a `WireError` with `415` or `406`. Media types match case-insensitively and may carry parameters. A missing `Accept` counts as `*/*`. `*/*`, `application/*` and `application/json` all allow JSON.
  - **`select_era(message: Message, headers: Mapping[str, str]) -> Era`.** Dispatch steps 3–5 for every method but `initialize`:
    - `MCP-Protocol-Version` is missing or holds invalid characters: `400`, `-32020`. It names a legacy revision: a legacy `Era`. It is `MODERN`: modern rules. Anything else: `400`, `-32022`, with `data == {"supported": list(SUPPORTED), "requested": <value>}`.
    - On a modern request, `Mcp-Method` must equal the body's method, and on `tools/call` `Mcp-Name` must equal `params.name` once `decode_name` has decoded it; otherwise `-32020`. A stray `Mcp-Name` on any other method is ignored.
    - A modern `params._meta` needs the protocol version, equal to the header (else `-32020`), and `clientCapabilities` as an object. `clientInfo` is optional but must be an object if present. Missing or malformed: `400`, `-32602`.
  - **`decode_name(value: str) -> str | None`.** `=?base64?<b64>?=` decodes to UTF-8 text; anything malformed gives `None`; a plain value comes back as it is.
  - **`jsonrpc_result(id, result) -> dict[str, Any]` and `jsonrpc_error(err: WireError) -> dict[str, Any]`.** The error carries an `id` only when one is known.
  - **`modern(result) -> dict[str, Any]`.** It adds `resultType: "complete"` and `_meta[META + "serverInfo"] = {"name": "workbench", "version": __version__}`.
  - **`initialize_result(requested: object) -> dict[str, Any]`.** `protocolVersion` is the requested revision if it is legacy, else `LEGACY_DEFAULT`. It also has `capabilities: {"tools": {"listChanged": False}}` and `serverInfo`.
  - **`discover_result() -> dict[str, Any]`.** `supportedVersions: list(SUPPORTED)`, the same `capabilities`, `instructions: INSTRUCTIONS`, `ttlMs: 3_600_000`, `cacheScope: "private"`.
  - **`tools_list_result(specs: Sequence[ToolSpec], caller: Caller, era: Era) -> dict[str, Any]`.** Each tool is `{name, title, description, inputSchema: spec.input_schema(caller), outputSchema: spec.output_model.model_json_schema()}`. On the modern era it adds `ttlMs: 300_000` and `cacheScope: "private"`. There is never a `nextCursor`.
  - **`tool_result(outcome: ToolOutcome) -> dict[str, Any]`** and **`tool_error(reason: str, text: str, extra: Mapping[str, object] | None = None) -> dict[str, Any]`.**
    - Success: `content` is `[{"type": "text", "text": <structured as JSON>}, *outcome.blocks]`, `structuredContent` is the structured result, and `isError` is `False`.
    - Error: `content` is `[{"type": "text", "text": text}]`, `structuredContent` is `{"reason": reason, **extra}`, and `isError` is `True`.
  - **`too_large() -> JSONResponse`.** `413`, `-32600`, "request too large", no `id`.
  - **`INSTRUCTIONS: Final`.** "workbench runs Python on the data each call carries, in a sandbox with no network, and deletes that data with the job. Call run_python with the code and its data files; tables come back as CSV and charts as PNG."
- Produces, in `workbench/api/mcp.py`:
  - `router = APIRouter(include_in_schema=False, route_class=EncodableRoute)`, not tagged `TOOL_TAG`.
  - `@router.post("/mcp") async def mcp(request: Request, caller: Annotated[Caller, Depends(require_caller)]) -> Response`. `GET` and `DELETE` on `/mcp` answer `405` with `Allow: POST` and a JSON-RPC error with no id.
  - The order inside `mcp`:
    1. A request with an `Origin` header: `403`.
    2. `check_media`.
    3. The body, read with `await request.json()` in a `try` of its own: a `ValueError` is `400`, `-32700`; an `HTTPException` 422 (too deep) is `400`, `-32600`; an `HTTPException` 413 is `too_large()`.
    4. `parse_message`.
    5. A notification: `202` with an empty body.
    6. `initialize`: `initialize_result`, whatever its headers.
    7. `select_era`.
    8. The method, by era. Modern: `server/discover`, `tools/list`, `tools/call`. Legacy: `ping` (which answers `{}`), `tools/list`, `tools/call`. Any other: `404`, `-32601`.

    Modern results pass through `modern()`.
  - `tools/call`, in this order:
    1. `params.name` must be a string that `registry.find(caller, name)` finds, else `400`, `-32602` "unknown tool".
    2. `principal_from_headers`: an `InvalidPrincipal` is `400`, `-32600`, with its text.
    3. `parse_purpose_header`: a `PurposeError` is `400`, `-32602` "invalid purpose".
    4. `clean_request_id`.
    5. `spec.input_model.model_validate(params.get("arguments", {}), context={"wall_cap": caller.wall_s})`. A `ValidationError` answers `tool_error("invalid_arguments", <compact as JSON>, compact(exc.errors()))`.
    6. Build a `ToolEnv` from `request.app.state` and run `await spec.run(body, env)`.
  - `call_failure(exc: Exception) -> tuple[str, str] | None`, the token and the text of an `isError` result:

    | Exception | Token | Text |
    |---|---|---|
    | `PurposeRequired` | `purpose_required` | "a stated purpose is required" |
    | `Busy` | `exc.reason` | `errors.BUSY` |
    | `RequestTooLarge` | `too_large` | `errors.TOO_LARGE` |
    | `NotCompleted` | `not_completed` | `exc.detail` |
    | `HTTPException`, `PathError`, `StoreError` | `refused` | `describe(exc)[1]` |

  - Two failures are no result. An `AuditWriteError` is `500`, `-32603` "audit unavailable", `data: {"reason": "audit_unavailable"}`. Any other `Exception` is `500`, `-32603` "internal error", `data: {"reason": "internal"}`, logged as `"an unexpected error ended %s on /mcp (%s)"` with the method (if it is a served name, else `"a request"`) and the type name.
  - Every answer is a `JSONResponse`, and none carries `Mcp-Session-Id`.
- In `body_limit.py`: on `MCP_PATH`, a declared `Content-Length` past `request_max` answers `mcp_wire.too_large()`. `main.py` includes `mcp.router`.

- [ ] **Step 1: Add the SDK.** Run `uv add --group dev "mcp>=2"`. Check that `git diff uv.lock` moves no runtime package (`fastapi`, `starlette`, `pydantic`, `uvicorn`, `prometheus-client`, `python-multipart`). If one moves, stop and report.
- [ ] **Step 2: Write the failing tests.** The helper is `rpc(client, method, params=None, *, id=1, version=MODERN, headers=HD, meta=True, name=None)`. It sends `Content-Type: application/json`, `Accept: application/json, text/event-stream`, `MCP-Protocol-Version`, `Mcp-Method` (and `Mcp-Name` on `tools/call`), and `params._meta` with the protocol version and `clientCapabilities: {}`. The executor is the `Scripted` one of `tests/api/test_tools_python.py`.

```python
# tests/api/test_mcp.py: the wire
def test_a_batch_is_an_invalid_request(client) -> None:  # [{...}] -> 400, error.code -32600, "id" not in the body
def test_invalid_json_is_a_parse_error(client) -> None:  # b"{not json" -> 400, -32700
def test_a_body_nested_past_the_depth_limit_is_an_invalid_request(client) -> None:  # MAX_DEPTH + 1 levels -> 400, -32600
def test_a_declared_body_over_2_mib_is_a_jsonrpc_413_before_it_is_read(settings) -> None:  # through the ASGI stack; body never asked for
def test_a_streamed_body_over_2_mib_is_a_jsonrpc_413(client) -> None:  # chunked generator -> 413, -32600, "request too large"
@pytest.mark.parametrize(("content_type", "accept", "status"), [("text/plain", None, 415), (None, None, 415),
    ("Application/JSON; charset=UTF-8", None, 200), ("application/json", "text/event-stream", 406), ("application/json", "text/html", 406),
    ("application/json", "*/*", 200), ("application/json", "application/*", 200)])
def test_media_types_are_matched_as_http_says(client, content_type, accept, status) -> None:  # a tools/list
def test_an_origin_header_is_403(client) -> None:
@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_are_405_with_allow_post(client, method) -> None:  # r.headers["allow"] == "POST"
def test_no_session_is_minted_and_one_sent_is_ignored(client) -> None:  # Mcp-Session-Id and Last-Event-ID sent -> 200; no mcp-session-id in any answer
def test_every_answer_is_json_and_a_notification_is_202_without_a_body(client) -> None:
@pytest.mark.parametrize("request_id", [1, 0, "req-1"])
def test_the_id_comes_back_as_sent(client, request_id) -> None:  # body["id"] == request_id and type(body["id"]) is type(request_id)
@pytest.mark.parametrize("request_id", [None, True, 1.5, {}, []])
def test_an_id_of_another_type_is_an_invalid_request(client, request_id) -> None:  # 400, -32600, "id" not in the body

# the dispatch
@pytest.mark.parametrize("headers", [{}, {"MCP-Protocol-Version": "2026-07-28"}])
def test_initialize_is_legacy_whatever_its_headers(client, headers) -> None:
    # params {"protocolVersion": "2025-06-18", ...} -> result {"protocolVersion": "2025-06-18",
    # "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "workbench", "version": __version__}}
@pytest.mark.parametrize("requested", ["2025-03-26", "2024-11-05", "2099-01-01", 7])
def test_initialize_naming_neither_legacy_revision_gets_2025_11_25(client, requested) -> None:
def test_a_legacy_session_works_with_the_version_header_alone(client) -> None:
    # initialize; notifications/initialized -> 202; ping -> {}; tools/list -> ["run_python"]; tools/call -> isError False;
    # no Mcp-Method sent; no resultType, ttlMs or cacheScope in any result
def test_a_request_without_the_version_header_is_a_header_mismatch(client) -> None:  # 400, -32020
def test_an_unserved_revision_is_32022_naming_both(client) -> None:  # "2025-03-26" -> data {"supported": [the three], "requested": "2025-03-26"}
@pytest.mark.parametrize("sent", [None, "tools/call", b"tools/list\xff"])
def test_a_modern_request_needs_mcp_method_matching_the_body(client, sent) -> None:  # tools/list -> 400, -32020
@pytest.mark.parametrize(("sent", "status"), [(None, 400), ("list_files", 400), ("=?base64?cnVuX3B5dGhvbg==?=", 200), ("=?base64?%%%?=", 400)])
def test_a_modern_tools_call_needs_mcp_name_matching_the_body(client, sent, status) -> None:
def test_a_stray_mcp_name_on_another_method_is_ignored(client) -> None:
@pytest.mark.parametrize("meta", [{}, {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
    {"io.modelcontextprotocol/protocolVersion": 7, "io.modelcontextprotocol/clientCapabilities": {}},
    {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": "x"},
    {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}, "io.modelcontextprotocol/clientInfo": "x"}])
def test_modern_meta_needs_the_version_and_client_capabilities(client, meta) -> None:  # 400, -32602
def test_a_meta_version_that_disagrees_with_the_header_is_32020(client) -> None:
@pytest.mark.parametrize(("method", "version"), [("resources/list", MODERN), ("ping", MODERN), ("server/discover", "2025-11-25")])
def test_a_method_the_era_does_not_serve_is_404_32601(client, method, version) -> None:

# results
def test_discover_names_the_revisions_the_caps_and_the_cache(client) -> None:
    # supportedVersions == ["2026-07-28", "2025-11-25", "2025-06-18"]; capabilities == {"tools": {"listChanged": False}};
    # instructions == INSTRUCTIONS (two sentences); ttlMs 3600000; cacheScope "private"; resultType "complete";
    # _meta["io.modelcontextprotocol/serverInfo"] == {"name": "workbench", "version": __version__}
def test_tools_list_is_the_callers_inline_tools_in_name_order(client) -> None:
    # names == ["run_python"]; ttlMs 300000; cacheScope "private"; "nextCursor" not in result;
    # inputSchema time_limit_s minimum 5 maximum 60; outputSchema present; "x-mcp-header" not in json.dumps(result)

# tools/call, one per row of §3.3
@pytest.mark.parametrize("tool", ["list_files", "read_document", "nowhere"])
def test_docints_call_of_a_workspace_tool_is_an_unknown_tool(client, tool) -> None:  # 400, -32602, "unknown tool"
@pytest.mark.parametrize("headers", [{"X-User-Id": None}, {"X-User-Id": "../x"}, {"X-Session-Id": "a/b"}])
def test_an_invalid_user_or_session_is_an_invalid_request(client, headers) -> None:  # 400, -32600
@pytest.mark.parametrize("purpose", ["reason=%ZZ", "reason=Prüfung"])
def test_a_malformed_purpose_is_invalid_params(client, purpose) -> None:  # 400, -32602, "invalid purpose"
@pytest.mark.parametrize("arguments", [{}, {"code": "print(1)", "time_limit_s": 4}, {"code": "print(1)", "time_limit_s": 61}, {"code": "print(1)", "x": 1}])
def test_arguments_that_do_not_validate_are_an_error_result(client, arguments) -> None:
    # 200; isError True; structuredContent["reason"] == "invalid_arguments"; its detail has type/loc/msg and no "input"
@pytest.mark.parametrize("reason", ["queue_full", "wait_timeout", "executor_busy"])
def test_a_queue_refusal_is_an_error_result_with_its_token(client, monkeypatch, reason) -> None:
def test_a_line_too_large_for_the_executor_is_too_large(...) -> None:
def test_code_that_could_not_complete_is_not_completed(...) -> None:  # executor status "timeout"
def test_another_refusal_is_refused(...) -> None:  # runner error "storage_full: ..."
def test_a_missing_required_purpose_is_purpose_required_and_recorded_denied(...) -> None:
def test_an_audit_failure_is_32603_audit_unavailable(broken_audit, client) -> None:  # 500; data {"reason": "audit_unavailable"}; no content
def test_anything_else_is_32603_internal_logged_by_method_and_type(client, monkeypatch, caplog) -> None:
    # run raises RuntimeError("/data/secret-path") -> 500, data {"reason": "internal"};
    # one log line holding "tools/call" and "RuntimeError", not "secret-path"
def test_a_method_name_is_never_logged_unless_served(client, monkeypatch, caplog) -> None:
def test_every_modern_result_carries_result_type_and_server_info(client) -> None:  # server/discover, tools/list, tools/call (ok and isError)
def test_identity_never_comes_from_meta(client, audit_lines) -> None:  # _meta {"user": "user-b"} -> actor.user is X-User-Id's
def test_the_mcp_door_needs_the_key(client) -> None:  # no Authorization -> 401 before any body is read
```

```python
# tests/api/test_mcp_sdk_client.py
def test_the_official_client_speaks_the_modern_era(app_with_scripted_executor) -> None:
    # serve the app with uvicorn on a socket bound to 127.0.0.1:0, in a thread, behind a pure ASGI recorder of each request's
    # MCP-Protocol-Version; the SDK's Streamable HTTP client, headers Authorization (docint key), X-User-Id, X-Session-Id:
    assert [tool.name for tool in tools] == ["run_python"]
    assert result.isError is False and result.structuredContent["stdout"] == "2 authors"
    assert recorded == {"2026-07-28"}  # every request was modern; no initialize fallback
```

```python
# tests/test_compose_defaults.py
def test_the_sdk_stays_in_the_dev_group() -> None:
    # pyproject: "mcp" only in [dependency-groups].dev, not in dependencies or the executor extra;
    # every runtime-stage `uv sync` in both Dockerfiles has --no-dev
```

- [ ] **Step 3: Run them to verify they fail.** Run `uv run pytest tests/api/test_mcp.py tests/api/test_mcp_sdk_client.py tests/test_compose_defaults.py -q`. Expected: FAIL (no `/mcp` route).
- [ ] **Step 4: Implement** `mcp_wire.py`, `mcp.py`, the `body_limit.py` hook and the router.
- [ ] **Step 5: Run them to verify they pass.** Run `make verify && make test`. Expected: all PASS. If the locked SDK has no client for `2026-07-28`, stop and report: never weaken the SDK test to the legacy era.
- [ ] **Step 6: Commit:** `git commit -m "feat(mcp): a stateless JSON-only MCP door for the modern and legacy revisions"`

### Task 7: Compose, `.env.example` and the docs (W8)

**Files:**
- Modify: `docker/compose.yaml`, `.env.example`, `docs/configuration.md`, `docs/permissions.md`, `docs/audit.md`, `docs/data-flow.md`, `docs/sandbox.md`, `CLAUDE.md`, `tests/test_compose_defaults.py`

**Interfaces:**
- Produces:
  - **Compose, the `api`'s `environment:`** (not `x-settings`, since the executor reads none of these), after `OPEN_WEBUI_WORKBENCH_KEY`:
    - `DOCINT_WORKBENCH_KEY: ${DOCINT_WORKBENCH_KEY:-}`
    - `DOCINT_WORKBENCH_TOOLS: ${DOCINT_WORKBENCH_TOOLS:-run_python}`
    - `DOCINT_WORKBENCH_WALL_S: ${DOCINT_WORKBENCH_WALL_S:-60}`
    - `DOCINT_WORKBENCH_SLOTS: ${DOCINT_WORKBENCH_SLOTS:-2}`
    - `AUDIT_REQUIRE_PURPOSE: ${AUDIT_REQUIRE_PURPOSE-query,disclose,combine}`. It is colon-less, as docint's is, so an empty value set in `.env` requires none.

    The comment on Open WebUI's key becomes: at least 32 characters when set; the api starts with either key.
  - **Compose, both services:** `ulimits: {core: 0}` on `api` and on `executor`.
  - **`.env.example`, in the federation format:**
    - The Required section goes: no setting is left in it (rule T3).
    - "Auth & identity" holds `# [secret] [shared: open-webui-service]` over `# OPEN_WEBUI_WORKBENCH_KEY=`, and `# [secret] [shared: docint WORKBENCH_API_KEY]` over `# DOCINT_WORKBENCH_KEY=`. Each has a few lines: at least 32 characters; unset, that caller's door is off; the api needs one of the two; `openssl rand -hex 32`.
    - The workbench section gains `# --- docint ---` with `# DOCINT_WORKBENCH_TOOLS=run_python`, `# DOCINT_WORKBENCH_WALL_S=60` and `# DOCINT_WORKBENCH_SLOTS=2`, one line of meaning each (§3.8).
    - "Audit & retention" gains `# AUDIT_REQUIRE_PURPOSE=query,disclose,combine`: the MCP door only; set it empty, uncommented, to require none; an unknown action stops the start.
  - **Docs: one short addition per page, nothing more (owner):**
    - `docs/configuration.md`: the two caller keys and the rule that one is needed; the three docint variables; `AUDIT_REQUIRE_PURPOSE` under "Audit and retention".
    - `docs/permissions.md`: "Callers and doors", which is §3.1's table, the `404` rule, and the tool list enforced on both doors; a matrix row for inline `run_python` (docint).
    - `docs/audit.md`: the `tool.run_python.inline` event, with target `inline_data`, its criteria, the purpose and request id taken from headers, and the `denied` event without a target. Replace "No purpose and no recipient in version 1" with its version 2 truth in a few lines, and add `config.audit`'s `callers` and `require_purpose`.
    - `docs/data-flow.md`: the §3.7 table, "What a docint call leaves".
    - `docs/sandbox.md`: inline inputs are root's and `0444` in the job folder. Core dumps are off (`RLIMIT_CORE` 0, `ulimits: core: 0`); the residual risk of `kernel.core_pattern` (§6) goes under Residual risks.
    - `CLAUDE.md`:
      - Callers: two callers, a door and a file form each.
      - The tool core: `registry.py`, `tools_inline.py`, `mcp.py` and `mcp_wire.py`.
      - "Adding a tool touches": now names `registry.WORKSPACE_TOOLS`, or for an inline tool `registry.INLINE_TOOLS` and `tools_inline.INLINE_SPECS`.
      - Audit: a purpose on the MCP door.
      - The v2 spec and this plan, beside v1's.
      - The `.env` line: either key.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_compose_defaults.py
CALLER_DEFAULTS = {"DOCINT_WORKBENCH_KEY": "", "DOCINT_WORKBENCH_TOOLS": "run_python", "DOCINT_WORKBENCH_WALL_S": "60", "DOCINT_WORKBENCH_SLOTS": "2"}

def test_the_api_alone_gets_the_callers_variables() -> None:
    spec = compose()
    api = spec["services"]["api"]["environment"]
    assert {key: default_of(api[key]) for key in CALLER_DEFAULTS} == {key: (key, value) for key, value in CALLER_DEFAULTS.items()}
    assert api["AUDIT_REQUIRE_PURPOSE"] == "${AUDIT_REQUIRE_PURPOSE-query,disclose,combine}"
    names = {*CALLER_DEFAULTS, "AUDIT_REQUIRE_PURPOSE"}
    assert names.isdisjoint(spec["services"]["executor"]["environment"]) and names.isdisjoint(spec["x-settings"])

def test_no_core_dumps() -> None:  # services api and executor: ulimits == {"core": 0}
def test_open_webuis_key_is_no_longer_required() -> None:
    # .env.example: no "[required]" tag line, no active "OPEN_WEBUI_WORKBENCH_KEY=" line; the commented one's tag line is
    # "# [secret] [shared: open-webui-service]"; "# DOCINT_WORKBENCH_KEY=" under "# [secret] [shared: docint WORKBENCH_API_KEY]"
```

- [ ] **Step 2: Run them to verify they fail.** Run `uv run pytest tests/test_compose_defaults.py -q`. Expected: FAIL.
- [ ] **Step 3: Implement** the compose and `.env.example` changes, then the docs.
- [ ] **Step 4: Verify.** Run these:
  - `make test`: all PASS, `tests/test_docs.py` included.
  - `docker compose --env-file .env.example -f docker/compose.yaml -f docker/compose.override.yaml config -q`: no output.
  - `python3 -B ../deploy/scripts/check_env_examples.py .`: the one expected finding is `T5 DOCINT_WORKBENCH_KEY`, until deploy's D1 rows are on `main`. Anything else is a defect to fix here. Run against all members at once, deploy's check may also report `X2` on open-webui-service's `[required]` `OPEN_WEBUI_WORKBENCH_KEY` until O1 lands; note it in the PR.
- [ ] **Step 5: Commit:** `git commit -m "feat(ops): docint's variables in compose and .env.example, no core dumps; short doc additions"`

### Task 8: Contract and isolation (W9)

**Files:**
- Modify: `docker/compose.test.yaml`, `tests/isolation/conftest.py`, `tests/isolation/test_isolation.py`, `tests/contract/test_open_webui_contract.py`

**Interfaces:**
- Produces:
  - `compose.test.yaml`, the `api`'s `environment:`: `DOCINT_WORKBENCH_KEY: synthetic-docint-test-key-not-a-secret-0000` (synthetic, at least 32 characters, not Open WebUI's) and `DOCINT_WORKBENCH_WALL_S: "20"`. The stack's wall time is 20, and docint's cap may not pass it, or the api does not start.
  - `tests/isolation/conftest.py`: `mcp_call(api_info, code: str, data: list[dict[str, str]], *, user: str = "a" * 64, session: str = "b" * 64) -> httpx.Response`. It is a modern `tools/call` of `run_python` with the docint key read from the api's environment.

- [ ] **Step 1: Write the tests**

```python
# tests/isolation/test_isolation.py (@pytest.mark.isolation)
MARKER = "synthetic-harbour-marker-7f3a"

def test_an_inline_job_leaves_nothing_on_the_volume(api_info, executor) -> None:
    # data [{"name": "passages.json", "content": json.dumps([{"n": 1, "text": MARKER}])}]; the code reads it, prints len(),
    # writes out/counts.csv and a matplotlib out/chart.png -> isError False, stdout "1", an image block and a resource block
    # docker exec executor: os.listdir("/data/jobs") == [], os.listdir("/data/staging") == [], not exists("/data/workspaces/docint")
    # docker exec api: SELECT COUNT(*) FROM jobs WHERE caller = 'docint' -> 1
    # MARKER is in neither `docker logs` of the api nor of the executor

def test_no_core_dumps_in_either_container(api_info, executor) -> None:
    # HostConfig.Ulimits of both == [{"Name": "core", "Hard": 0, "Soft": 0}];
    # an inline job printing resource.getrlimit(resource.RLIMIT_CORE) prints "(0, 0)"
```

```python
# tests/contract/test_open_webui_contract.py (@pytest.mark.contract)
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_mcp_through_the_proxy_is_not_found(admin: Account, method: str) -> None:
    # admin.terminal(method, "/mcp", ...) with Accept, MCP-Protocol-Version 2026-07-28, Mcp-Method tools/list
    # (and a tools/list body on POST) -> 404
```

- [ ] **Step 2: Run them.** Run `make test-isolation`, then `make test-contract`. Expected: both PASS.
- [ ] **Step 3: Verify the rest.** Run `make test`. Expected: PASS (`test_the_test_stack_keeps_nothing_of_a_developers` unchanged).
- [ ] **Step 4: Commit:** `git commit -m "test: the MCP door through Open WebUI's proxy, an inline job on the test stack"`

### Task 9: Release v0.2.0 (W10)

**Files:**
- Modify: `pyproject.toml` (`version = "0.2.0"`), `uv.lock`

- [ ] **Step 1: Bump the version** and run `uv lock`. Run `uv run pytest tests/test_version.py -q`. Expected: PASS.
- [ ] **Step 2: Run the full set.** Each must be green, and the PR quotes the output:
  - `make verify`
  - `make test`
  - `make test-executor-root`
  - `make test-isolation`
  - `make test-contract`
  - `python3 -B ../deploy/scripts/check_env_examples.py .`: clean once deploy's D1 has merged; until then, only `T5 DOCINT_WORKBENCH_KEY`.
- [ ] **Step 3: Commit:** `git commit -m "chore: release v0.2.0"`
- [ ] **Step 4: Open the PR.** Push `feat/v2-mcp-door`, open the PR and confirm that CI ran on its last push. Merge only after deploy's D1. On merge, `release-tag.yml` mints `v0.2.0`; never tag by hand. The federation-wide end-to-end checks (§8) belong to the federation plan. Here, §8's door checks 3 and 6 are covered by Tasks 1, 6 and 8.

---

## Self-review

- **Coverage.**
  - §3.1 is Tasks 1–2. §3.2 is Tasks 2 and 5. §3.3 is Task 6. §3.4 is Tasks 3–5. §3.5 is Tasks 1 and 4. §3.6 is Task 5. §3.7 is Tasks 3, 4, 7 and 8. §3.8 is Tasks 1 and 7.
  - §7's W1–W10 are Tasks 1–9. W5 and W6 are one task, Task 5.
  - §7's "tests that matter" each name a test above: the config rules; the door `404` before any body; an Open WebUI user id with docint's key; `require_tool` refusing a reduced list; every §3.3 row and token; the SDK client; §3.4 and §3.6 rule by rule; the caller gate and its waiting cap; the 5 s wait; staging on every ending; inline files as root and `0444`; the registry equal to the routes; the nine operations; `tests/test_docs.py`; `ulimits`.
- **Types.** These are the same names in every task that uses them: `CallLimits`, `TakenOutputs`, `TakenFile`, `SkippedOutput`, `Taker.take`, `submit_inline`, `ToolSpec.input_schema`, `ToolEnv`, `ToolOutcome`, `PurposeRequired`, `NotCompleted`, `read_code_run`, `fit_python`, `InlineFile` and `MCP_PATH`.
