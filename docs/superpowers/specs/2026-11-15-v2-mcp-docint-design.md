# workbench version 2 — the MCP door and docint as a caller

Status: approved by the owner · 2026-11-15

This is the design of workbench version 2: an MCP door on workbench, and docint as its second
caller. It builds on the version 1 design (`2026-11-13-workbench-design.md`, §11). Decisions
are marked **(owner)** when the project owner made them and **(design)** when this document
makes them; the builder may revisit a **(design)** call with a stated reason, never an
**(owner)** one.

## 1. Purpose and scope

### What version 2 adds

- An MCP door (Streamable HTTP) on workbench, built from its tool registry, so that later AI
  services can use workbench as their central tool repository (owner).
- docint as a second caller. Within a chat turn its model may run Python on what the turn
  retrieved: passages, keyword counts, topic listings. Tables and charts appear in the answer
  and can be added to the report (owner).
- A key, a tool list and limits per caller (owner).
- A new Report Builder artifact type in docint, `analysis` (owner).

### Owner decisions

1. docint uses workbench as an agent tool.
2. docint gets only `run_python`, enforced per caller. Open WebUI keeps all nine tools.
3. Data goes inline with each call. workbench keeps it only for that job and deletes it with
   the job. docint records the hand-over as a `disclose` with purpose.
4. Results come back as the `analysis` artifact: table, charts, code and provenance,
   exportable to md, html, pdf, json and zip.
5. The MCP server is hand-written and lean: stateless, JSON-only, protocol revision
   `2026-07-28` plus the legacy `2025-11-25` and `2025-06-18`. The official SDK sits in the
   dev group, for a conformance test only.
6. docint's user id at workbench is a keyed pseudonym of the owner name, stable while the
   shared key stays the same (§4.2).
7. Documentation stays proportionate: each repo's doc task is a few short additions to the
   pages it has.

Earlier decisions that still bind: data tools stay in docint, and workbench never joins
`data-net`; no caller sees another caller's workspaces; the audit trail records by reference
only, never content; fail closed; airgap-first; no GPL or AGPL dependency; workbench logs hold
counts and types only; synthetic test data only.

### Non-goals

- No workspace for docint. Nothing of a docint call stays in `workspaces/`.
- No change for Open WebUI. It stays on the terminal door with its nine tools.
- No data tools in workbench. docint's `search`, `read`, `keyword` and `topics` stay in docint.
- No identity across callers. Each caller's user ids are its own namespace.
- No workspace tools, uploads, SSE, sessions or subscriptions over MCP.

## 2. Facts it rests on (verified)

- A FastAPI route decorator returns the plain function, so `workbench/api/tools.py:run_python`
  can be called without HTTP. Refusals are `HTTPException`s, or domain exceptions that
  `workbench/api/errors.py:_answer` maps.
- In docint, a tool's `fetch` must not read the turn's context or ledger
  (`docint/agents/turn/tools.py:ToolWork`), so a call's data is built in `prepare`.
- `docint/agents/assessment/bounded.py:start_bounded` passes no deadline into the work. A
  process has 8 search slots, and abandoned work keeps its slot until it ends.
- docint writes a chat turn's `query` event after the turn (`docint/core/api.py:_record_chat_turn`).
  `docint/core/audit.py:Operation.child` needs that event first.
- The chat routes use no transfer meter. `/stream_query` settles a failure with
  `audit.settle_quietly`, `/query` through `settle_failed_operations`. When a stream's client
  goes away, `_aiter_sync_gen` stops reading, but the worker thread runs the turn to its end.
- Stateless turns run through the same `SessionManager.stream_agent_turn`, with
  `session_id=None` and `persist=False` (`docint/core/rag.py:RAG.stream_stateless_agent_turn`).
- Report snapshots come from the client (`docint/core/api.py:ReportItemIn`). Only thumbnails
  are frozen by the server. `docint/core/retention_sources.py:report_item_sources` treats an
  unknown artifact type as collection-wide.

## 3. workbench

### 3.1 Callers and doors (design)

`workbench/api/auth.py:CallerRegistry.from_settings` builds the callers. `Caller` gains `door`,
`files`, `wall_s` and `slots`. Keys and lists come from `.env` (§3.8).

| Caller | Door | Files | Tools | Wall cap | Running jobs |
|---|---|---|---|---|---|
| `open-webui` | terminal | workspace | all nine | `WORKBENCH_JOB_WALL_S` | not capped |
| `docint` | mcp | inline | `run_python` | `DOCINT_WORKBENCH_WALL_S` (60) | `DOCINT_WORKBENCH_SLOTS` (2) |

- `KeyGate` authenticates as now, in constant time over all callers. Then it checks the door,
  before any body is read.
- An MCP caller gets `404` on every path but `/mcp`, `/healthz` and `/metrics`. A terminal
  caller gets `404` on `/mcp`. Open WebUI's proxy forwards any path, so
  `/api/v1/terminals/<id>/mcp` answers `404` too. Every such `404` is
  `{"detail": "not found"}` (`errors.NOT_FOUND`).
- The tool list is enforced on both doors; today nothing reads `Caller.tools`. Each terminal
  tool route depends on `require_tool("<name>")`, which answers `404` for a tool not on the
  caller's list.
- `workbench/api/openapi.py:tool_openapi` builds one document per caller. Open WebUI's keeps
  its nine operations.
- Identity travels in HTTP headers, as on the terminal door: `X-User-Id` (required on
  `tools/call`), `X-Session-Id` (optional, else `default`), `X-Audit-Purpose`, `X-Request-ID`.
  One helper, `auth.py:principal_from_headers`, serves both doors.
- Identity never travels in `_meta`, which a model may shape.
- A caller is a namespace: users are `(caller, user_id)` in the queue and the job table. The
  docint caller has no folder under `workspaces/`.
- The api starts with at least one caller key, either one. Two callers with the same key stop
  the start. `OPEN_WEBUI_WORKBENCH_KEY` thus becomes optional, a change to
  `workbench/config.py` (§3.8).

### 3.2 One tool core (design)

- `workbench/api/registry.py:ToolSpec` describes a tool: `name`, `title`, `description`, the
  input and output models, `form` (`workspace` or `inline`) and `run(body, env)`.
- The nine workspace specs are derived at app creation from the routes tagged `TOOL_TAG`. They
  are not served on MCP in version 2. `auth.ALL_TOOLS` becomes `registry.WORKSPACE_TOOLS`.
- Inline specs are declared in `workbench/api/tools_inline.py`. Version 2 has one,
  `run_python`.
- Every tool on a caller's list must exist in the caller's form, or the api does not start.
- `ToolEnv` carries `p`, `caller`, `settings`, `store`, `audit`, `jobs`, `purpose` and
  `request_id`. The MCP door builds it from `app.state` and the headers. The terminal routes
  keep their signatures.
- `workbench/api/errors.py` gains `describe(exc)` and `compact(errors)`, taken out of `_answer`
  and `_invalid`. Both doors refuse with the same short fixed texts.
- The part of `tools.py:run_python` after the inputs are chosen (budget, `_python_payload`,
  `_CodeRun`, hints) becomes a helper both forms use. `_fit_python` works on any model with
  `stdout` and `stderr`.

### 3.3 The MCP door (owner: hand-written; design: the rules)

`POST /mcp` has a router of its own: `route_class=EncodableRoute`, in no schema, not tagged
`TOOL_TAG`. The middleware order is unchanged: `ViewHardening`, `QuietErrors`, `KeyGate`,
`BodyLimit` (2 MiB), then the route. The code is `workbench/api/mcp.py` (route, dispatch) and
`workbench/api/mcp_wire.py` (versions, header checks, JSON-RPC shapes; no I/O).

| Revision | Era | How a request states its version | Methods |
|---|---|---|---|
| `2026-07-28` | modern | `_meta` `io.modelcontextprotocol/protocolVersion` and the `MCP-Protocol-Version` header | `server/discover`, `tools/list`, `tools/call` |
| `2025-11-25`, `2025-06-18` | legacy | `initialize` negotiates it; later requests send `MCP-Protocol-Version` alone | `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call` |
| `2025-03-26` and older | refused | its `initialize` gets `2025-11-25` back; its later requests carry no version header | — |

Dispatch, in this order:

1. The body is read (wire rules). An array is a batch: `-32600`.
2. `initialize` is legacy, whatever its headers: it carries no `MCP-Protocol-Version`. One
   naming neither legacy revision, `2025-03-26` included, gets `2025-11-25`, as the legacy rules
   ask: `protocolVersion`, `capabilities.tools.listChanged: false`, `serverInfo`, no session.
3. Every other request needs `MCP-Protocol-Version`. A legacy revision there selects the legacy
   rules, `2026-07-28` the modern ones.
4. A modern request needs `Mcp-Method`, and `Mcp-Name` on `tools/call`. Each must match the
   body; a base64 `Mcp-Name` (`=?base64?…?=`) is decoded first. A stray `Mcp-Name` on another
   method is ignored: the spec is silent on it.
5. A modern `_meta` needs `protocolVersion` and `clientCapabilities`; `clientInfo` is optional.

Wire rules:

- One JSON-RPC message per POST.
- `Content-Type` must be `application/json`, parameters allowed (`; charset=utf-8`), else
  `415`. `Accept` must allow `application/json` (`*/*` and `application/*` do), else `406`.
- A request with an `Origin` header is `403`. No browser is a caller.
- `GET` and `DELETE` on `/mcp` are `405` with `Allow: POST`.
- No `Mcp-Session-Id` is ever minted. One that is sent is ignored, and so is `Last-Event-ID`.
- Every answer is `application/json`, body errors included (JSON-RPC, no `id`; W7 has the
  hooks). A notification is `202` with no body.
- `server/discover` returns `supportedVersions`, `capabilities: {tools: {listChanged: false}}`,
  `serverInfo` in `_meta` (`workbench` and its version), a two-sentence `instructions`,
  `ttlMs` 3600000 and `cacheScope: "private"`.
- `tools/list` returns the caller's tools in its form, in name order, with `ttlMs` 300000 and
  `cacheScope: "private"`, and no `nextCursor`. The spec allows a list to vary by
  authorization. `run_python`'s schema gives `time_limit_s` a `minimum` of 5 and a `maximum` of
  the caller's wall cap.
- Every modern result carries `resultType: "complete"` and `serverInfo` in its `_meta`.
- No parameter uses `x-mcp-header`: header values are visible to intermediaries.
- In CI, the SDK's client (dev group only, never in an image) tests the modern era. The legacy
  sequences are written by hand in the tests.

| Cause | Answer |
|---|---|
| invalid JSON | `400`, `-32700` |
| a batch, or a body nested past `routing.MAX_DEPTH` | `400`, `-32600` |
| a body over 2 MiB | `413`, `-32600` "request too large" |
| a required header missing, one that disagrees with the body, or one with invalid characters | `400`, `-32020` (HeaderMismatch) |
| `_meta` without `protocolVersion` or `clientCapabilities`, or with one malformed | `400`, `-32602` |
| a revision the door does not serve | `400`, `-32022`, with `data.supported` (the three revisions) and `data.requested` |
| an unknown method | `404`, `-32601` |
| an unknown tool, or one not on the caller's list | `400`, `-32602` "unknown tool" |
| `X-User-Id` missing or invalid, `X-Session-Id` invalid | `400`, `-32600` |
| `X-Audit-Purpose` malformed | `400`, `-32602` "invalid purpose" |
| arguments that do not validate, `time_limit_s` out of range included | a result, `isError: true`, the compact error list; `invalid_arguments` |
| the queue refused | a result, `isError: true`, the short text; `queue_full`, `wait_timeout` or `executor_busy` (`jobs.py:Reason`) |
| the line is too large for the executor | a result, `isError: true`, "request too large"; `too_large` |
| the code could not complete (time, memory or another limit) | a result, `isError: true`, the short text; `not_completed` |
| the tool refused otherwise (`HTTPException`, `describe(exc)`) | a result, `isError: true`, the short text; `refused` |
| a required purpose is missing | a result, `isError: true`, "a stated purpose is required"; `purpose_required`; recorded as `denied` (§3.6) |
| `AuditWriteError` | `500`, `-32603` "audit unavailable", `data.reason: audit_unavailable`, no content |
| anything else | `500`, `-32603` "internal error", `data.reason: internal`, logged by method, route and type alone |

An `isError` result carries its token as `structuredContent.reason`. The texts may change; the
tokens are the contract, and callers map the token, never the text.

### 3.4 Inline `run_python` (owner: inline data; design: the shapes)

Request, `tools_inline.py:InlinePythonRequest`, unknown fields refused:

| Field | Rule |
|---|---|
| `code` | 1 to 50 000 characters |
| `data` | 0 to 8 entries `{name, content}`; `name` matches `^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.(json\|csv\|txt\|md)$` and is unique; all `content` together at most 512 KiB in UTF-8 |
| `time_limit_s` | optional; 5 to the caller's wall cap, the schema's `maximum`. A value outside is refused, never clamped, so a client's error stays visible |

- The bounds aim at the executor's 1 MiB line and the 2 MiB body. Where JSON escaping still
  passes them (quote-heavy text, control characters), `jobs.py:ExecutorClient.run`'s existing
  "request too large", or the `413`, covers it.
- `workbench/protocol.py:JobRequest` gains `inline_inputs: list[InlineFile]`. Each name is
  checked like a file name: flat, not `out`, unique. The texts together stay within 512 KiB.
- `input_files` is empty, so `_copy_inputs` returns at once and never opens `workspaces/`;
  `Store.add_job` makes no folder either.
- `supervisor.py:Supervisor._write_inline` writes each entry into the job's working folder,
  root's and `0444`, like a copied input. The data is never in `spec.json`.
- The data lives in memory for the request and in the job folder; §3.7 lists every place. It
  goes with the folder (`Supervisor._remove`; `_retry_leftovers` when a removal failed). Nothing
  is staged on the api's side.
- Outputs are ephemeral. `JobQueue.submit` takes a taker in place of a keeper.
- `JobQueue._take`, in a worker thread, reads each file of `staging/<job-id>/`. It applies the
  path check of `_register`, opens with `O_NOFOLLOW`, takes regular files only, and reads at
  most the file's cap plus one byte.
- Only `.png`, `.csv`, `.txt`, `.md` and `.json` come back. Any other file is dropped and
  counted before the caps apply, and the hint names the types that come back.
- A `.png` must start with the PNG signature. A text that is not strict UTF-8 is dropped and
  counted.
- Files go back in this order: `.png`, then `.csv`, then the rest, each in name order.
- Return caps, in that order: at most 8 files; an image up to 512 KiB, a text up to 256 KiB,
  1 MiB raw in all. A file past a cap is dropped and counted, and the hint says why (for a
  chart: `plt.savefig(..., dpi=100)`).
- Then the taker writes the audit event. Only then does anything go out. `JobQueue._settle`
  removes the staging folder as before.
- No output is registered: no `files` row, no `register_output`, no `workspaces/docint/`.
- A resource's URI holds the file name percent-encoded.
- `stdout` and `stderr` stay within `WORKBENCH_RESULT_MAX_KB`.
- Tables come back as CSV text. The api parses no content; docint parses the CSV.
- Open WebUI's `run_python` is unchanged: its route, schema, description, kept outputs and
  audit operation.

Result (synthetic):

```json
{"resultType":"complete","isError":false,
 "content":[
  {"type":"text","text":"<structuredContent as JSON>"},
  {"type":"image","data":"<base64>","mimeType":"image/png","annotations":{"audience":["user"]}},
  {"type":"resource","resource":{"uri":"workbench://jobs/<job-id>/out/counts.csv",
   "mimeType":"text/csv","text":"author,posts\nuser-a,12\nuser-b,7\n"}}],
 "structuredContent":{"stdout":"2 authors","stderr":"","truncated":false,"job_id":"<32 hex>",
  "files":[{"name":"chart.png","mime":"image/png","bytes":48213,"returned":true}],"hint":null,
  "reason":null},
 "_meta":{"io.modelcontextprotocol/serverInfo":{"name":"workbench","version":"0.2.0"}}}
```

### 3.5 Limits per caller (design)

- A job's wall time is the least of `time_limit_s`, the caller's `wall_s` and
  `WORKBENCH_JOB_WALL_S`. The code gets 9/10 of it.
- An MCP caller waits at most 5 s for its turn (`jobs.py:MCP_QUEUE_WAIT_S`). A docint call
  takes at most docint's `WORKBENCH_TIMEOUT`, 75 s, inside the turn's `AGENT_TIME_BUDGET_S`,
  120 s by default (§4.2).
- `jobs.py:_CallerGate` holds a capped caller to `slots` running jobs, taken after the user's
  turn and before the global slot. It also counts the caller's waiting calls, at most
  `slots × (WORKBENCH_QUEUE_MAX // WORKBENCH_EXECUTOR_SLOTS)`, 8 of 16 by default.
  `JobQueue._admit` counts a call as waiting when the user's turn, the gate or the global slot
  is taken, and refuses past either share at once (`queue_full`). The per-user share alone
  cannot do this: every docint user has a pseudonym of its own.
- `DOCINT_WORKBENCH_SLOTS` must be less than `WORKBENCH_EXECUTOR_SLOTS`, or the api does not
  start. One slot always stays free for other callers.
- Open WebUI is not capped, as in version 1.
- A docint call that gives up does not stop its job: it ends at its wall time and is recorded,
  and no slot is retired (§6).

### 3.6 Audit (design)

- Event key `run_python_inline`, recorded as `combine` / `tool.run_python.inline`
  (`workbench/api/audit.py:OPERATIONS`).
- `actor`: `{"user": <X-User-Id>, "via": "service"}`.
- `target`: `kind: inline_data`, `count` (the data entries), `container: chat:<X-Session-Id or
  default>`; no ids. `AuditLog.record` hard-codes `kind: file` today, so it gains a target kind
  and a count apart from the ids.
- `criteria`: `caller`, `job` (the job's id), `input_bytes`, `outputs` (files returned),
  `dropped`; `refused: <reason>` on a refusal, the §3.3 token.
- `purpose` from `X-Audit-Purpose` (`audit_runtime.parse_purpose_header`); `request_id` from
  `X-Request-ID` (`audit_runtime.clean_request_id`), a new parameter of `AuditLog.record`.
- The event is written once the job has answered, before anything of the answer goes out. A
  write that fails sends nothing (§3.3).
- Never recorded: the data or a digest of it, file names, the code, `stdout`, a refusal's text.
- The job id and the request id tie the event to docint's `disclose`. A digest would be a
  dictionary oracle: a short entry, a few words and counts, can be guessed and tested.
- `AUDIT_REQUIRE_PURPOSE` (`audit_runtime.parse_required_actions`) applies on the MCP door
  only; compose sets `${AUDIT_REQUIRE_PURPOSE-query,disclose,combine}`, colon-less, as docint
  does. The terminal door keeps version 1's rule: Open WebUI cannot state a purpose.
- A call without a purpose where one is required is recorded as `denied`, without a target, as
  docint records it (`docint/core/audit.py:begin`).
- `config.audit` adds `callers`, each with its name, door, tools, wall cap and slots (never a
  key), and `require_purpose`, sorted, as docint lists it.

### 3.7 What a docint call leaves

| Where | What | How long |
|---|---|---|
| the api's memory | the request, the data, the outputs | the request; freed memory is not cleared |
| the executor's memory | the request line with the data | the job; freed memory is not cleared |
| `jobs/<job-id>/` | the data as root's `0444` files; the job's `out/` and `home/` | the job. A folder whose removal failed stays, root's alone, and is tried again every 5 minutes until it goes or the executor restarts (`Supervisor._remove`, `_retry_leftovers`) |
| `staging/<job-id>/` | the outputs | until the job settles; retention removes a leftover after an hour |
| free blocks of `workbench-data` | deleted job files | until overwritten; encrypting the host's disk is the operator's |
| `workbench.db`, `jobs` | job id, caller, user pseudonym, session pseudonym, tool, status, times | `WORKBENCH_RETENTION_DAYS` |
| the audit chain | §3.6 | obs-plane's retention |

- No core dump holds the data: jobs run with `RLIMIT_CORE` 0 (`workbench/executor/sandbox.py`),
  and compose sets `ulimits: core: 0` on the api and the executor (else the daemon's limit).

### 3.8 Configuration (design)

Flat variables, one per setting. Compose names every variable of the api, env-sync shares one
secret per variable, and the checker compares flat defaults.

| Variable | Default | Meaning |
|---|---|---|
| `OPEN_WEBUI_WORKBENCH_KEY` | empty: Open WebUI's door is off | Open WebUI's key, at least 32 characters; no longer `[required]` |
| `DOCINT_WORKBENCH_KEY` | empty: docint's door is off | docint's key, at least 32 characters; `[secret] [shared: docint WORKBENCH_API_KEY]` |
| `DOCINT_WORKBENCH_TOOLS` | `run_python` | comma list; each tool needs an inline form |
| `DOCINT_WORKBENCH_WALL_S` | 60 | at most `WORKBENCH_JOB_WALL_S` |
| `DOCINT_WORKBENCH_SLOTS` | 2 | less than `WORKBENCH_EXECUTOR_SLOTS` |
| `AUDIT_REQUIRE_PURPOSE` | empty; compose `query,disclose,combine` | actions that need a stated purpose, MCP door only |

- `OPEN_WEBUI_WORKBENCH_KEY` changes: `workbench/config.py:_api_key` and
  `CallerRegistry.from_settings` require one of the two keys, not this one. `.env.example`
  drops its `[required]` tag; compose already defaults it to empty; the config tests change.
- The new variables go into the api's `environment:`, not `x-settings`: the executor reads
  none of them. `load_settings` checks their rules (wall cap, slots) in the api role only, so
  an executor with `WORKBENCH_EXECUTOR_SLOTS=2` still starts.

## 4. docint

### 4.1 The tool (design)

`docint/agents/turn/workbench_tool.py:WorkbenchPythonTool`, offered as `run_python`.

- Arguments: `code` (string, required); `passages` (integers, optional; left out, every passage
  shown so far); `title` (string, at most 80 characters, optional).
- The model is told: the folder holds `passages.json`, a list of
  `{n, text, file, page, time, author, network, kind}`, and `keyword_checks.json` and
  `topics.json` when the turn made such checks; tables go as CSV and charts as PNG into `out/`,
  at most 4 each; print a short summary; one call per round, two per turn; there is no network
  and nothing can be installed.

`prepare`, on the loop thread:

- refuses a second call in one round (`call_limit`, "call it again next round") and a third
  accepted call in one turn (`call_limit`, "no more runs in this turn"), and a number the
  evidence does not hold (`no_such_passage`);
- builds `passages.json` from the ledger as it stood, through `ctx.llm_view(node)`
  (`docint/core/rag.py:agent_llm_view`), whose clone still holds the hidden keys: only keys not
  in the view's `excluded_llm_metadata_keys` are read (table below);
- builds `keyword_checks.json` and `topics.json` from `ToolContext.facts`, a new list that
  `ToolRegistry.finish` fills with `{action, detail, **trail}` for each reply a tool's work
  answered with a trail (a topics listing is no `PassageReply`, so `answered` does not hold it);
  a reply answered from memory adds nothing, so no step counts twice;
- fills each file in order (passages by number, facts by call) while the next entry fits its
  cap: `passages.json` 384 KiB, `keyword_checks.json` 32 KiB, `topics.json` 64 KiB. A size is the
  UTF-8 bytes of the file as written (`json.dumps`, `ensure_ascii=False`, no indent). The reply
  states what was left out;
- keeps the provenance in docint: each passage's point id, file hash, file name and page;
- returns a `ToolWork` whose `fetch` records the disclosure and calls the client (§4.3).

| Field | Visible key it comes from (`core/rag.py:LLM_VISIBLE_METADATA_KEYS`) |
|---|---|
| `n` | the passage's number in the ledger |
| `text` | the node's text without metadata, cut to 4 000 characters |
| `file` | `filename`, else `origin.filename` |
| `page` | `page`, else `page_number`, else `origin.page_number` |
| `time` | `reference_metadata.timestamp`, else `start_ts` |
| `author` | `reference_metadata.author`, else `speaker` |
| `network` | `reference_metadata.network` |
| `kind` | `reference_metadata.type`, else `docint_doc_kind` |

A missing value is `null`.

The call counts live on the tool instance. `toolset_for` builds the tools fresh for each turn,
and `prepare` runs on the loop thread, one call after another. `RoundCalls.prepare` first calls
a new `ToolRegistry.begin_round()`, which resets the round count of each tool that defines
`begin_round` (optional, as `recall_key` is). The round count takes every prepared call. The
turn count takes only the calls `prepare` accepted with a `ToolWork`: a call refused for
`call_limit` or `no_such_passage` leaves the turn's two intact.

`answer`:

- writes a reply within `ctx.budget.result_chars`: a head line, `stdout` cut in the middle,
  each table's shape with up to 5 rows, each chart's name, the tail of `stderr` if any, and
  "The user sees these tables and charts below your answer as Analysis k; refer to it by that
  name and cite the passages [n] the code used.";
- names a `.txt`, `.md` or `.json` output with its size; only CSV tables and PNG charts are
  stored;
- sets `found` 0, `informative`, `detail` to the title, and the trail
  `{"analysis": k, "passages": n, "tables": t, "charts": c}`;
- appends the analysis to `ToolContext.artifacts`, a new list.
  `docint/agents/turn/loop.py:AgentResult` gains `analyses`.

Rules:

- The tool defines no `recall_key`. Its reply is a plain `ToolReply`, which the registry never
  recalls, so a repeated call runs again. The call limit bounds repeats.
- New `ErrorKind` tokens: `call_limit`, `code_failed`, `unavailable`, `too_much_data`.
  `no_capacity` and `no_time` are reused.
- At most 2 calls per process wait on workbench at once: `fetch` takes a process-wide semaphore
  without waiting, else answers `no_capacity`. workbench holds at most 2 of the 8 search slots.
- `docint/agents/turn/toolsets.py:toolset_for` places the tool after `topics` and before
  `read`, on `documents` and `all`. Never on `visual`, never on a scoped turn.
- `SessionManager.stream_agent_turn` passes `persist and session_id is not None` into
  `docint/core/rag.py:RAG.agent_turn`, with the owner and the turn's deadline. Only then, and
  only while workbench is configured and discovered (§4.2), is the tool built. A stateless turn
  passes `False`: its analyses would never be stored.
- When the room for tool replies is short, `run_python` is the first tool given up, before
  `topics` and `keyword`.
- A prompt line `agent_tools_python.txt` joins the tools guide, in `docint/utils/prompts/en/`
  and `de/`.
- Only a run with a table or a chart becomes an analysis.

Validation. Each analysis reaches the validator as a computed fact, as keyword checks do:

- `docint/core/api.py:_turn_validation` renders the facts from the payload's `analyses`, since
  the trail holds no `stdout`: per analysis the title, passages used, the first 600 characters
  of `stdout` and the tables' shapes, within `ANALYSIS_CHECKS_CHARS` (1 600).
- An `analysis_checks` parameter runs through `_validation_payload`, `_turn_validation`,
  `_assessment_validation` and `docint/agents/types.py:RetrievalResult` into
  `docint/agents/generation.py`. There `ResultValidationResponseAgent.finalize`'s no-sources
  guard counts it as checks, and `_build_prompt` shows it under a heading of its own.
- `docint/agents/assessment/split.py:cited_sources` falls back to every source only without
  keyword, topic or analysis checks.
- `response_validator.txt` and `response_validator_assessment.txt` (`docint/utils/prompts/en/`
  and `de/`) accept a figure such a fact gives.

### 4.2 The client (owner: identity; design: the rest)

`docint/utils/workbench_client.py`, a thin `httpx` client in the pattern of
`nextext_client.py`. It speaks `2026-07-28` only.

Discovery:

- `RAG.agent_turn` asks the client before it builds the tools. Without a cached discovery, the
  client sends `server/discover`, then `tools/list`, within 5 s together. A call's `t` is never
  spent on it.
- Discovery succeeds when workbench serves `2026-07-28` and lists `run_python` with
  `inputSchema.properties.time_limit_s.maximum`, the cap. Only then is the tool offered.
- Each result is cached for its `ttlMs` and fetched again on the first turn after it expires.
  One lock makes discovery single-flight: concurrent turns wait for the one in progress.
- A failed discovery offers no tool on that turn. The next try comes on a later turn, at most
  once per 60 s. A call that cannot reach workbench, or whose `time_limit_s` is refused as an
  argument error (a cap lowered since), drops the cache. One warning per change of state.

Requests:

- Every request sends `Authorization: Bearer`, `Content-Type: application/json`,
  `Accept: application/json, text/event-stream`, `MCP-Protocol-Version`, `Mcp-Method`, and
  `_meta` with `protocolVersion`, `clientInfo` (docint's version) and `clientCapabilities: {}`.
- `Mcp-Name` goes on `tools/call` only, where the spec requires it.
- `X-User-Id` is a pseudonym of `principal.effective_owner`: HMAC-SHA256 keyed with the shared
  key `WORKBENCH_API_KEY`, over `workbench-user/v1`, a NUL byte and the name, as 64 hex digits.
  No edge user name leaves docint. With an admin acting, it is the owner's; docint's own event
  names the admin, `on_behalf_of` the owner.
- The pseudonym is stable while the key is. A rotation gives new ones and breaks nothing:
  workbench stores nothing per docint user, and old records keep the old pseudonym.
- `X-Session-Id` is the same HMAC over `workbench-session/v1`, a NUL byte and the session id, so
  a client-chosen id (`QueryIn.session_id`) never reaches workbench verbatim.
- `X-Audit-Purpose` and `X-Request-ID` come from `forward_headers`. The chat routes bind no
  `forwarding_of` today, so the reason falls back to `docint ingest`. They now bind
  `forwarding_of(op.context)`; with no stated purpose the reason is `docint chat`, through a
  new `system_reason` argument.

Time:

- The tool takes the turn's deadline when it is built: `turn_started_at` plus
  `AGENT_TIME_BUDGET_S`, never later than the loop's own.
- `fetch` computes `t = min(WORKBENCH_TIMEOUT, time left − 15 s)` when it starts. With `t`
  under 20 s it sends nothing (`no_time`). The 15 s leave the turn room to go on.
- The call sends `time_limit_s = min(t − 12, cap)`. The 12 s hold the 5 s wait at workbench
  and 7 s for start-up, the executor's cleanup, the outputs, the audit write and the transfer.
  The least value sent is 8, inside workbench's range.
- One deadline bounds the whole call. `fetch` runs it with `anyio.run` over an
  `httpx.AsyncClient`, inside `anyio.fail_after(t)`. Past it the call is cancelled and answers
  `unavailable`. The phase timeouts (connect 3 s, write 10 s, pool 3 s) stay as guards.
- With the defaults, a call made with 90 s or more left sends 60, the cap, and ends within
  75 s.

Answers:

- The answer is read as a stream. It is refused past 4 MiB, or when it is not
  `application/json`. Refusing SSE deviates from the client's MUST to support it: workbench
  never streams, and this client talks to no other server.
- An answer that is not a JSON-RPC message is `unavailable`. An image must be base64 with the
  PNG signature, at most 512 KiB.
- An `isError` result is mapped by its `structuredContent.reason`, never its text: `too_large`
  is `too_much_data`; `queue_full`, `wait_timeout` and `executor_busy` are `no_capacity`;
  `not_completed` is `code_failed`; any other token, and any JSON-RPC error, is `unavailable`,
  logged by its token or code.
- The log holds statuses and kinds, never code, data or output. A call is no `ai_call`: it
  calls no model.

### 4.3 Audit (design)

- `docint/utils/audit_disclose.py`, stdlib only like `audit_meter.py`: `disclosing(op)` is a
  context manager that binds a ledger in a context variable, and `record_disclosure(...)` adds
  to it from any thread. It follows the work into the worker and search threads, which run in a
  copy of the context.
- `/query` and `/stream_query` declare `also=("ai_call", "disclose")` while workbench is
  configured. `audited(...)` is evaluated at import, so `load_workbench_env()` is read then; a
  key set later needs a restart.
- Both routes bind `forwarding_of(op.context)` and `disclosing(op)` around the turn.
  `/stream_query` enters them in `event_generator`, so they also close when the stream is
  cancelled.
- `fetch` records its disclosure before it sends: point ids, counts, `data_bytes`, then the job
  id once the answer names it. Once the ledger is sealed, `record_disclosure` refuses, and
  `fetch` answers `unavailable` without sending.
- `_record_chat_turn` writes one child per disclosure right after the primary event, before the
  final frame: `disclose` / `chat.workbench`; `target` `kind: chunk` with the point ids handed
  over; `recipient: workbench:run_python`; `criteria` `workbench_job`, `passages`,
  `keyword_checks`, `topic_listings`, `data_bytes`; `outcome` `ok`, or `error` when no answer
  came.
- `disclosing(op)` closes the same way on every exit: success, failure, a client gone. It
  settles the operation as failed when nothing settled it (`settle_quietly`, `incomplete`),
  writes each disclosure not yet written beneath the primary or failure event, and seals the
  ledger. A later round of a turn whose client went away thus sends nothing.
- A disclosure written at the seal while its call is still in flight has `outcome: error`,
  `criteria.incomplete` and no `workbench_job`; workbench's own event names the job.
- `GET /sessions/{session_id}/analyses/{analysis_id}` is `query` / `session.analysis`, target
  `kind: analysis`. Adding to a report stays `modify` / `report.item_add`.

### 4.4 The `analysis` artifact (owner: the type; design: the rest)

Storage:

- `docint/core/state/turn_analysis.py:TurnAnalysis`, table `turn_analyses`: `id`, `turn_id`
  (indexed), `position`, `payload` (JSON), `created_at`. `Turn.analyses` deletes with its turn.
  `create_all` makes the table; no column migration is needed.
- `SessionManager._persist_turn` writes them with the turn. A turn written over clears them, as
  it clears citations.
- They go with their turn and conversation, so chat retention applies. The passages handed
  over are among the turn's sources, which already set the session's deadline.
- Bounds: 2 per turn; 4 charts of at most 512 KiB each and 1 MiB together, as workbench
  returns them; 4 tables of at most 500 rows and 32 columns, cells cut to 500 characters,
  `row_count` kept.

Snapshot (synthetic):

```json
{"schema":1,"title":"Posts per author","question":"Who wrote most about the harbour?",
 "code":"import pandas as pd ...","stdout":"2 authors",
 "tables":[{"name":"counts.csv","columns":["author","posts"],
  "rows":[["user-a","12"],["user-b","7"]],"row_count":2,"truncated":false}],
 "charts":[{"name":"chart.png","data_uri":"data:image/png;base64,...","bytes":48213}],
 "provenance":{"session_id":"<uuid>","turn_idx":3,"collection":"harbour-case",
  "sources":[{"citation_index":1,"chunk_id":"<point id>","file_hash":"<sha256>",
   "filename":"harbour-report.pdf","page":4}],
  "inputs":{"passages":12,"keyword_checks":1,"topic_listings":0},
  "workbench":{"job_id":"<32 hex>","version":"0.2.0","tool":"run_python"},
  "created_at":"2026-11-15T10:00:00+00:00"}}
```

Frames and routes:

- The final frame of `/stream_query` and the body of `/query` carry `analyses` in full, each
  with its stored `id`: `_persist_turn` returns the ids, and `QueryOut` gains `analyses`.
- `GET /sessions/{session_id}/history` carries a summary per analysis: `id`, `index`, `title`,
  the tables' names and shapes, the charts' names and sizes.
- The SPA reads a full analysis from the new route, through `apiFetch`. The route is under
  `/sessions/`, which nginx and the dev proxy already pass.

Adding to a report:

- The SPA posts `artifact_type: analysis`, `dedupe_key: analysis:<id>` and
  `snapshot: {"analysis_id": <id>}`.
- The server builds the snapshot from the stored row. The caller must own it, and the session's
  collection must be the report's. Other fields from the client are ignored.
- Each chart must match `^data:image/png;base64,` and its cap. Anything else is `404`. The
  batch route does the same.
- `report_item_sources` reads `provenance.sources[].file_hash`. Without hashes, the item is
  collection-wide.

Rendering, `docint/core/state/report_render.py`:

- `ARTIFACT_ANALYSIS = "analysis"`. `SECTION_ORDER`: summary, topics, chat, analysis,
  entities, hate speech. Anchor `sec-analyses`. Items are numbered.
- Headings and labels go into `ui_strings.py`, in `en` and `de`.

| Format | Rendering |
|---|---|
| md | title, question, the code in a `python` block, `stdout` fenced, tables as GFM through `_md_cell`, charts as `![name](data:image/png;base64,…)` with the name escaped as alt text, a provenance line |
| html, pdf | tables as `<table>`, charged rows × columns to the report's one `_TableBudget` in markdown-it's `state.env` (10 000 cells, 32 columns); a table past the room left falls to its CSV in `<pre>`, as a 500 × 32 table (16 000 cells) always does; charts through `_html_evidence_figure`, PNG data URIs only; the PDF still fetches `data:` only |
| json | unchanged: the stored snapshot |
| zip | `analyses.csv` (one row each: session, turn, title, question, job, workbench version, sources, tables, charts), `analysis-<n>/<table>.csv`, `analysis-<n>/<chart>.png`; safe member names; text cells through `_csv_text` |

Frontend:

- `AgentAction` gains `run_python` in `docint/agents/types.py`, `frontend/src/api/types.ts` and
  `frontend/src/components/chat/ChatTurn.tsx` (its label, and a marker drawn with an existing
  `@infra/ui` icon).
- `ArtifactType` gains `analysis`.
- `frontend/src/components/chat/AnalysisCard.tsx` sits below the answer: title, a table
  preview, the charts (with the preview dialog), the code behind a disclosure, the provenance
  line and the add-to-report control. A turn read from history fetches its analyses lazily.
- `frontend/src/components/report/ReportSection.tsx` renders the section. New strings go into
  `en` and `de`.

### 4.5 Network and configuration (design)

- The backend joins `workbench-net` (alias `docint-backend`) in `docker/compose.yaml`. The
  frontend does not. The Makefile's `NETWORKS` gains `workbench-net`.
- `docint/utils/env_cfg.py:WorkbenchClientConfig` and `load_workbench_env()` read:

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_API_KEY` | empty: the tool is off | the shared key, at least 32 characters, else docint does not start; `[secret] [shared: workbench DOCINT_WORKBENCH_KEY]` |
| `WORKBENCH_API_BASE` | compose `http://workbench-api:8000` | workbench's api |
| `WORKBENCH_TIMEOUT` | 75 | seconds one call may take |

- docint works without workbench. Without a key it offers no tool. With workbench unreachable,
  a call answers `unavailable` and the turn goes on; later turns offer no tool until discovery
  succeeds again (§4.2).

## 5. Federation

- deploy `env-sync.conf` gains two rows:

```
workbench-docint-key  workbench  DOCINT_WORKBENCH_KEY  gen
workbench-docint-key  docint     WORKBENCH_API_KEY     gen
```

- deploy ADR 0006, "docint calls workbench over an MCP door", records: the door; a key, tool
  list and limits per caller; inline data deleted with the job; docint's backend on
  `workbench-net` as a new trust edge; the `analysis` artifact.
- Its alternatives: the official SDK (`mcp` 2.x, MIT: about 15 more packages in the api image,
  three native), uploads into a workspace, a sandbox in docint, a network of its own for MCP
  callers, Open WebUI over MCP.
- ADR 0005 gains one line, "Extended by 0006." 0005 left version 2 to a decision of its own.
- deploy `docs/tech-stack.md` and `docs/tech-stack.de.md` list docint's backend on
  `workbench-net`.
- The infra workspace's `CLAUDE.md`, not a repository, names docint's backend on the fourth
  seam.
- Unchanged: open-webui-service (its connection, tools and key; one comment, O1); obs-plane
  (the same emitter `workbench` / `api`; the verifier lists no operations or target kinds; the
  same scrape target); the bring-up order, inference → data → obs → workbench → apps → edge.

## 6. Residual risks

- docint records the hand-over after the data has left, as for model calls, from a ledger in
  memory. A docint killed between send and settle, or one that cannot write the turn's failure
  event (logged, as `settle_quietly` logs it), loses those `disclose` events. workbench's own
  event, written before it answers, still holds the job, purpose and request id.
- Retrieved documents can steer the code that runs on them. The executor has no network. What
  comes back is capped, checked as PNG, escaped in exports and guarded in CSV.
- docint's backend and Open WebUI can reach each other on `workbench-net`. docint believes
  identity headers only with `DOCINT_EDGE_SECRET` set, which production sets.
- The docint key is a bearer secret. Its holder can run code as docint under any user id. It
  reaches no workspace: the door and the caller namespace stop it.
- workbench holds the key the pseudonyms are made with, so it can test a guessed name against
  a pseudonym. The same operator runs docint, whose trail names the user.
- MCP asks a server to stop work on a cancelled request (SHOULD). workbench does not: uvicorn
  does not cancel a JSON route, and `Supervisor._handle` runs a job on after its socket closes.
  An abandoned call runs to its wall time, and that user's next call waits behind it
  (`WORKBENCH_JOBS_PER_USER`).
- On a busy host the executor's cleanup can outlast the 7 s margin (1-26 s measured,
  `workbench/executor/sandbox.py`). The call then ends at its deadline as `unavailable`, and
  workbench still records the job.
- A host whose `kernel.core_pattern` pipes core dumps to a handler runs it whatever the limit.
  systemd-coredump honours a limit of 0; other handlers may not. The operator's measure: the
  host keeps a file pattern or systemd-coredump, or turns dumps off (`Storage=none` in
  `coredump.conf`).
- An analysis holds at most 1 MiB of charts, about 1.4 MB as data URIs, in docint's session
  database; a turn, two analyses. The bounds and chat retention limit it.
- workbench returns embedded resources without the `resources` capability, which the spec
  recommends. A client that insists would need tables in `structuredContent`, parsed in the
  executor.

## 7. Delivery

Three pull requests, in this order, then a one-line fourth. Each repo's documentation is a few
short additions to the pages it has (owner); no new long documents.

### deploy (first: rule T5 needs the rows)

- D1 `env-sync.conf` rows, ADR 0006, the line in 0005, the tech-stack lines. Test: `make test`.

### workbench v0.2.0

- W1 Callers and config: `workbench/config.py` (either key, at least one; the docint
  variables and their rules, api role only; `AUDIT_REQUIRE_PURPOSE`); `workbench/api/auth.py`
  (`CallerRegistry.from_settings` rows, door check and its `404` body, `require_tool`,
  `principal_from_headers`).
- W2 Registry and errors: `api/registry.py`; `api/errors.py` (`describe`, `compact`);
  `api/openapi.py` per caller; `require_tool` on the nine routes.
- W3 Protocol and supervisor: `workbench/protocol.py` (`InlineFile`, `inline_inputs`);
  `executor/supervisor.py:Supervisor._write_inline`.
- W4 Queue: `api/jobs.py` (inline inputs, `_take` and its type allow-list, `_CallerGate` with
  its running and waiting counts, wall and wait per call, `MCP_QUEUE_WAIT_S`).
- W5 Inline tool: `api/tools_inline.py`; the shared helper in `api/tools.py`.
- W6 Audit: `api/audit.py` (the operation, `AuditLog.record`'s new parameters, the `denied`
  event); `api/main.py` (`config.audit`).
- W7 MCP door: `api/mcp.py`, `api/mcp_wire.py`, the router mounted in `api/main.py`. `mcp.py`
  reads the body through `EncodableRequest.json()` in its own `try`: a parser `ValueError` is
  `-32700`, the depth `422` is `-32600`, the `413` raised while the body streams in is a
  JSON-RPC `413`. `api/body_limit.py:BodyLimit` answers a `Content-Length` past 2 MiB on `/mcp`
  with `mcp_wire.too_large()`.
- W8 `docker/compose.yaml` (the new variables; `ulimits: core: 0` on the api and the
  executor), `.env.example` (Open WebUI's key no longer `[required]`), and short additions to
  `docs/configuration.md`, `docs/permissions.md`, `docs/audit.md`, `docs/data-flow.md`,
  `docs/sandbox.md` and `CLAUDE.md`.
- W9 Contract and isolation: `/mcp` through Open WebUI's proxy answers `404`; an inline job
  leaves nothing on the test stack.
- W10 Release: `pyproject.toml` 0.2.0, `mcp` in the dev group, `uv lock`.

Tests that matter:

- `tests/test_config.py`: either key alone starts the api, none does not; the docint rules
  stop the api, never the executor.
- `tests/api/test_callers.py`: every door `404` comes before any body is read; an Open WebUI
  user id sent with docint's key reaches nothing; a reduced list is refused by `require_tool`.
- `tests/api/test_mcp.py`: each dispatch step and each row of the §3.3 table, the `reason`
  tokens included; docint's `tools/call` of a workspace tool.
- `tests/api/test_mcp_sdk_client.py`: the official client against the api on loopback.
- `tests/api/test_tools_inline.py`: §3.4 and §3.6 rule by rule; nothing out when the event
  cannot be written.
- `tests/api/test_jobs.py`: the caller gate and its waiting cap across many users; the 5 s
  `wait_timeout`; staging removed on success, error and cancellation.
- `tests/executor/test_protocol.py` and the root tests of `tests/executor/test_supervisor.py`:
  inline files root's `0444`, unchangeable by the job, unreadable to other slots, gone after it.
- The registry equals the tool routes; Open WebUI's document keeps its nine operations;
  `tests/test_docs.py` and `tests/test_compose_defaults.py` (`ulimits`) pass.

Checks: `make verify`, `make test`, `make test-executor-root`, `make test-isolation`,
`make test-contract`, `python3 -B ../deploy/scripts/check_env_examples.py .`

### docint (next minor)

- I1 Config and network: `docint/utils/env_cfg.py` (read at import), `docker/compose.yaml`,
  `Makefile`, `.env.example`.
- I2 Client: `docint/utils/workbench_client.py` (discovery and its cache, the pseudonyms, the
  deadline); `system_reason` in `docint/utils/audit_forward.py`.
- I3 Tool and turn: `docint/agents/turn/workbench_tool.py`; `facts`, `artifacts` and
  `begin_round` in `tools.py` and `calls.py`; `toolsets.py`, `loop.py`; the gate in
  `docint/core/rag.py` and `session_manager.py`; the prompt line in `docint/utils/prompts/en/`
  and `de/`.
- I4 Validator: `analysis_checks` through `_validation_payload`, `_turn_validation`,
  `_assessment_validation`, `RetrievalResult` and `generation.py`; the four validator prompts.
- I5 Audit: `docint/utils/audit_disclose.py` (`disclosing(op)`, the seal),
  `docint/core/audit.py`, the chat routes.
- I6 Storage and route: `turn_analysis.py`, `turn.py`, `_persist_turn` (returning the ids),
  `QueryOut`, the history, `GET /sessions/{session_id}/analyses/{analysis_id}`.
- I7 Reports: the snapshot built by the server, `report_render.py`, `retention_sources.py`,
  `ui_strings.py`.
- I8 Frontend and `AgentAction` (`docint/agents/types.py` included), as §4.4 lists them.
- I9 Short additions to `docs/retrieval-and-agents.md`, `docs/reports.md`, `docs/audit.md` (a
  route row and a few lines beside "Calls to Nextext"), `docs/configuration.md` and
  `docs/api-reference.md`; the version.

Tests that matter:

- `tests/test_workbench_client.py` (`MockTransport`): §4.2 rule by rule, the `reason` token
  mapping pinned; concurrent turns share one discovery; no content in the log.
- `tests/test_agent_workbench_tool.py`: §4.1 rule by rule. A parallel pair in one round runs
  one and refuses the other, and the next round's call still runs; a refusal for `call_limit`
  or `no_such_passage` leaves the turn's two intact.
- `tests/test_agent_toolsets.py`, `tests/test_agent_rag.py`: the gate, a stateless turn
  included; the first tool given up.
- `tests/test_agents_generation.py`: analysis checks in the no-sources guard and the prompt.
- `tests/test_audit_api.py`: the `disclose` children on success, on failure and on a cancelled
  stream; the in-flight one at the seal; no send after it; the chain verifies.
- `tests/test_api_reports.py`, `tests/test_report_render.py`: §4.4 rule by rule.
- `tests/test_agent_persistence.py`, `tests/test_session_history_collection_scope.py`:
  analyses stored, cleared with a replaced turn, deleted with the conversation, gated like the
  history.
- Frontend: `ChatTurn.test.tsx`, `AnalysisCard.test.tsx`, `ReportSection.test.tsx`.

Checks: `uv run pre-commit run --all-files`, `uv run pytest`,
`cd frontend && pnpm test && pnpm build`, `python3 -B ../deploy/scripts/check_env_examples.py .`

### open-webui-service (after workbench v0.2.0)

- O1 The `.env.example` comment on `OPEN_WEBUI_WORKBENCH_KEY` says workbench refuses to start
  without it. From v0.2.0 that holds only for a key shorter than 32 characters: reword it in a
  small PR, or note it as a follow-up there.

## 8. End-to-end checks

1. `make env ENV_GENERATE=1` writes one key into workbench's and docint's `.env`. `make up`
   brings the stack up in order.
2. Open WebUI: upload, `run_python` and `display_file` work as before.
   `/api/v1/terminals/<id>/mcp` answers `404`.
3. With docint's key, `/files/list` and `/tools/run_python` answer `404`. With Open WebUI's
   key, `/mcp` answers `404`.
4. In docint, a `documents` turn searches, then is asked to count posts per author and chart
   them. The trail shows `run_python`. The answer names Analysis 1. The card shows the table,
   the chart and the code. Validation does not flag the counts.
5. workbench's trail holds one `combine` / `tool.run_python.inline` event with a `count`, the
   job, purpose and request id, and no digest. docint's holds `query` / `chat.query` and a
   `disclose` / `chat.workbench` child with the same request id and job. obs-plane's verifier
   accepts both chains.
6. On `workbench-data` there is no `workspaces/docint/`. `jobs/` and `staging/` are empty after
   the call, and one job row remains. The api's log holds counts and types only.
7. The analysis goes into a report, and the report is exported as md, html, pdf, json and zip.
   The table renders, the chart is a data URI, the zip holds the CSV and the PNG, and the PDF
   renders without a network.
8. The session is reloaded: the card loads through the analysis route. Another user, or
   another collection, gets `404`.
9. Limits through docint: code past `time_limit_s` answers a timeout; a second `run_python` in
   one round answers `call_limit`; a docint timeout retires no slot; every call ends within its
   `t`, and on an idle host it also answers. Driven at the door with docint's key: five calls
   at once never hold more than 2 executor slots while Open WebUI's jobs still start, and data
   over 512 KiB is refused as invalid arguments.
10. The shared key is changed in both `.env` files and both stacks restart. docint's calls work
    at once, under new pseudonyms.
11. workbench is stopped. docint's chats still answer: a call in flight answers `unavailable`,
    and later turns offer no `run_python`. Started again, the tool is back on a turn within
    about a minute.
12. A stream is closed while its turn runs `run_python`. docint's trail holds the turn's `error`
    event with a `disclose` child for every call sent, and workbench gets no call after it.
