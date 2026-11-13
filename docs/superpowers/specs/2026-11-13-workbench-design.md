# workbench — design

Status: draft for review · 2026-11-13

This is the design of `workbench`, the federation's shared tool tier. Its first caller is
Open WebUI (open-webui-service), which becomes an agentic ad-hoc assistant ("the office
assistant"). A later version adds docint as a second caller. Decisions are marked
**(owner)** when the project owner made them and **(design)** when this document makes
them; the builder may revisit a **(design)** call with a stated reason, never an
**(owner)** one.

## 1. Intent

A plain chat model is too limited for real office work. Users drop a spreadsheet, a few
documents and a report template into a chat and want answers, analyses and a filled-in
report back. Open WebUI can call tools, but its own tools are Python that its backend runs
with `exec`, unsandboxed, with every user's chats in reach; open-webui-service therefore
switches them off (v0.13.0). The tools have to run somewhere else, behind a boundary that
can be documented for data protection and does not depend on trusting tool code.

workbench is that somewhere. It holds a short-lived workspace per user, runs typed tools
over it, and executes free Python in a sandbox with no network. Every file it stores is
recorded in the federation's audit trail and deleted after 7 days.

### Where it sits between the two products (owner)

| | docint | open-webui-service + workbench |
|---|---|---|
| Role | data analysis platform for larger projects | chat agent and ad-hoc assistant |
| Data | lasting collections, vector search, Report Builder, stated purpose | short-lived workspace, 7 days, no collections |
| Typical use | set up a project, ingest a corpus, work it for weeks | drop files into a chat, ask, download the result |

The two products share no data store. Both use workbench as the shared tool tier (docint
from version 2).

### New federation invariant (owner)

**Tool execution is shared, never embedded.** Apps do not run code sandboxes or office
tooling of their own; they call workbench. This mirrors "inference is shared, never
embedded". Scope (owner): workbench holds **compute tools** (reading documents, Python,
templates, writing tables and documents). **Data tools stay with the data's owner**:
docint's `search`, `read`, `keyword` and `topics` stay in docint, and workbench never
joins `data-net`.

## 2. Goals, non-goals, anti-goals

### Goals (version 1)

1. Open WebUI users attach files to a chat, and the model can list, read and search them.
2. The model analyses tables with free Python (pandas and friends) in a sandbox (owner).
3. The model inspects a user's report template and fills it, with a source for every filled
   value. Templates are of two kinds (owner): existing Word templates (`.docx`, `.dotx`) with
   content controls or legacy form fields, and templates made for the assistant with
   `{{ field }}` placeholders (DOCX and XLSX).
4. The model writes tables (XLSX, CSV) and text documents (DOCX, Markdown) that the user can
   preview and download from the chat.
5. Every boundary is structural and documented well enough to feed the operator's record of
   processing and impact assessment.

### Non-goals

- **OCR.** `read_document` reads PDFs with a text layer only. Pages without text are
  reported as needing preprocessing elsewhere, never returned empty (owner).
- A shell, network access from tools, web search, sending e-mail, writing to other systems.
- Memory across workspaces, or keeping anything past the retention period.
- MCP and docint as a caller (version 2, §11).
- Per-user containers, multi-host scaling.
- Converting DOCX or PPTX to PDF for previews (the browser renders DOCX itself).
- Templates without fields: free documents with headings or "[insert name]" text, and Excel
  forms with a label beside each value cell (owner: not version 1). The system prompt tells
  users what a template needs.

### Anti-goals (failure even if it "works")

- One user reaching another user's files by any path: a forged chat id, a crafted file
  path, a symlink, a job reading outside its workspace.
- A deletion without its audit record, or an audit or ops log line that carries file
  content, file names typed by users in free text, or extracted text.
- Code from a file or the model reaching the network, the API's credentials, or the host.
- Unbounded growth: of disk, memory, queue length, or output sent back to the model.
- A scanned page answered with silence, so the model invents its content.
- Anything that fetches at runtime (models, packages, telemetry): the federation is
  airgap-first.

## 3. Constraints from the federation

- **Repo conventions** (infra `CLAUDE.md`): Python with uv, ruff, pyrefly, pytest,
  pre-commit; `make verify` and `make test`; Docker files under `docker/`; a Makefile that
  includes the vendored `make/common.mk`; `scripts/bundle-lib.sh` vendored for
  `make bundle` / `make bundle-dev`; `.env.example` in the federation format (deploy
  `docs/runbooks/env-sync.md`); versioning in `pyproject.toml`, tag minted by the shared
  `release-tag` workflow; every workflow `uses:` pinned to a full SHA.
- **Container hardening** (deploy ADR 0001): non-root `USER`, `read_only`, tmpfs where
  needed, `cap_drop: [ALL]`, `no-new-privileges`. The executor's supervisor needs a few
  capabilities to switch user ids (§6); that exception is listed and justified in
  `docs/sandbox.md`.
- **State**: an external volume `workbench-data`, created by `make volumes`, removed only by
  `make nuke` behind a confirm. `docker compose down -v` never destroys it.
- **Audit** (deploy ADR 0003): the vendored `audit_event.py` and `audit_runtime.py` from the
  `.github` hub (`configs/audit/`), drift-checked in CI; one hash chain per writing process;
  references and counts, never content. obs-plane's verifier accepts chains only from listed
  `(compose project, compose service)` pairs, so the API's events come from project
  `workbench`, service `api`, and `("workbench", "api")` is added to
  `obs-plane/audit/verifier.py` `DEFAULT_EMITTERS` in the same round.
- **Airgap**: every library is in the images; nothing installs at runtime (no `pip` inside a
  job, no `micropip`). `make bundle` ships the images.
- **Single host**, like the whole federation. Dozens of users, a handful active at once.

## 4. Architecture

```
browser ─ edge-plane :8443 ─ edge-guard ─ Open WebUI ── agent loop (native tool calling → vllm-router)
                                              │
                                              │ terminal-server HTTP: Bearer key, X-User-Id, X-Session-Id
                                              │ over workbench-net (only callers and obs-plane join it)
                                              ▼
                                  workbench / api  ─── workbench-data (workspaces, spool, retention state)
                                              │      audit chain → container stdout → obs-plane
                                              │ job handoff on the shared volume (no TCP)
                                              ▼
                                  workbench / executor  (network_mode: none; one uid per slot)
```

- **`api`** (FastAPI, one async process): the caller-facing door, authentication, workspace
  and path management, the audit chain, the retention sweep, the job queue and `/metrics`.
  It never parses user files itself.
- **`executor`**: a supervisor with a fixed number of slots. It runs every job that touches
  file content: Python, document reading, search, template inspection and filling, output
  rendering. Parsers of untrusted files are attack surface and CPU-heavy, so both concerns
  put them here.
- **Job handoff** (design): over the shared volume, as a spool directory or a Unix socket
  on it. The executor has no network at all. The exact mechanism is the builder's.
- **Network** (design): a new external network `workbench-net`, the federation's fourth
  seam. Members: `api`, the callers (`open-webui` now, docint's backend in version 2) and
  obs-plane as scraper. `api` does not join `inference-net`; it calls no model.
- **Bring-up order**: inference → data → obs → **workbench** → apps → edge.

### Why the terminal-server door (verified against Open WebUI v0.13.3)

Open WebUI connects "terminal servers" via `TERMINAL_SERVER_CONNECTIONS`. It is the only
integration that brings files in and out the way the assistant needs:

- With `config.chat_uploads: "filesystem"` the browser uploads chat attachments through
  Open WebUI's proxy (`/api/v1/terminals/{id}/…`) straight to the server. Open WebUI stores
  nothing, builds no vectors (`middleware.py:1691`), and edge-guard's intake refusal does
  not apply. The model is told `<file type="filesystem" id="<abs path>" …/>`.
- The model's tools are the operations of the server's `/openapi.json`.
- `display_file` shows a server file as a card with preview and download.
- MCP in Open WebUI passes only model arguments, no files (`middleware.py:2703-2711`), and
  renders only images from results, so it cannot serve this use.

upstream open-terminal is not used: it gives a shell, its key grants everything, and its
`SECURITY.md` says it does not isolate users; per-user containers need the separately
licensed "Terminals" orchestrator. workbench implements only the subset Open WebUI calls,
with typed tools and no shell.

## 5. The terminal contract workbench implements

Open WebUI v0.13.3 calls the following (paths relative to the server URL). Everything not
listed answers `404`. Open WebUI's proxy forwards every method and path from the browser,
so this allow-list is workbench's, not Open WebUI's.

### Handshake and per-request discovery (fast: no queue, no file parsing)

| Call | Must return | Notes |
|---|---|---|
| `GET /api/v1/policies` | **non-2xx** (`404`) | a 2xx makes Open WebUI treat the server as an orchestrator |
| `GET /api/config` | `200` JSON with `features.system: true` | verify step and every chat request (3 s timeout) |
| `GET /system` | `{"prompt": "<system prompt>"}` | the assistant's system prompt lives here (§8) |
| `GET /openapi.json` | the tool spec, with `paths` | fetched with **only** `Authorization`, no user headers; without Redis on every chat request, so serve it from memory |
| `GET /files/cwd` | `{"cwd", "home"}` absolute | 5 s timeout |
| `POST /files/cwd {path}` | `{"cwd"}` | only paths inside the caller's own root |
| `GET /files/read?path=` | `{"content"}` | also probed for `<home>/AGENTS.md` per chat; answer `404` when absent |
| `GET /skills`, `GET /skills/read?name=` | `[]`, `404` | connection errors here are not caught upstream, so they must answer |

### File browser (called by the browser through the proxy)

`GET /files/list?directory=` → `{dir, writable, entries:[{name, type, size, modified}]}`;
`POST /files/upload?directory=` (multipart field `file`) → `{path, size}`;
`GET /files/view?path=` → raw bytes with the right `Content-Type` (`preview=true` for
DOCX/PPTX answers without PDF, so the browser renders it); `DELETE /files/delete?path=`;
`POST /files/mkdir {path}`; `POST /files/move {source, destination}`. Optional, may answer
`404`: `POST /files/archive`, `/files/search`, `/files/glob`, `/files/matches`,
`/system/info`, `/api/terminals/user/output`.

### Tool rules

- Every operation in `/openapi.json` with an `operationId` becomes a tool. All handshake and
  file-browser routes are therefore **excluded from the schema**; only the tools in §7 are in
  it, each with an explicit `operation_id` and an explicit `type` on every parameter
  (Optional query parameters otherwise reach the model as strings).
- `POST` tool calls carry a JSON body. A status ≥ 400 reaches the model as
  `{"error": "HTTP error N: <body>"}`, so error bodies are short, actionable and free of
  content.
- `display_file`: Open WebUI replaces its spec with its own (`path`, `inline`) and calls
  `GET /files/display?path=` → `{path, exists, name, mime_type}`, then fetches the bytes via
  `/files/view`.
- Paths are absolute in a virtual namespace (§6), never host paths.
- Upstream reference for every row above: the contract research of 2026-11-13 (Open WebUI
  v0.13.3 `utils/tools.py:1223-1994`, `utils/terminals.py`, `routers/terminals.py`,
  `routers/configs.py:316-419`, `middleware.py:323-366, 712-995, 1415-1452`,
  `src/lib/apis/terminal/index.ts`, `MessageInput.svelte:1051-1185`,
  `ToolOutputFile.svelte:99-258`).

### Contract test (design)

workbench pins the Open WebUI image it is verified against (the same tag and digest as
open-webui-service; Dependabot bumps both). A CI job boots that image with workbench and
exercises the handshake, the spec load (the expected tools appear, no others), a tool call,
an upload and a download through Open WebUI's proxy, and `display_file`. A bump that
changes the contract fails there before it merges; open-webui-service's CLAUDE.md names the
dependency next to the retention schema guard. The harness is the builder's.

## 6. Identity, workspaces and isolation

### Callers and identity

- Open WebUI's connection uses `auth_type: "bearer"` with its own key,
  `OPEN_WEBUI_WORKBENCH_KEY` on both sides. It is not the forwarded-JWT secret, which the
  router also holds. Keys are compared in constant time.
- Version 1 has one caller, but the code keeps a **caller registry** (name, key, allowed
  tools, identity scheme) so version 2 adds docint without restructuring.
- `X-User-Id` (Open WebUI's opaque account id) is trusted from a caller with a valid key:
  only Open WebUI holds the key and only callers are on `workbench-net`. It is required on
  every route except the handshake ones and validated as an id (it becomes a directory
  name). Tool calls carry no signed user token (verified), so the audit actor in version 1
  is this opaque id, as in open-webui-service's retention sidecar; an admin resolves it in
  Open WebUI's user table.
- `X-Session-Id` is the chat id. In the browser proxy the browser can set it freely, and a
  plain terminal gets no per-chat context from Open WebUI (orchestrator-only, verified).
  So **the user is the access boundary; the chat is only a folder.**

### Workspace layout (design)

- Real path: `<volume>/workspaces/<caller>/<user-id>/<chat-id>/` with `out/` subfolders per
  job; spool and retention state elsewhere on the volume.
- Virtual path shown to Open WebUI: `/workspace/<chat-id>/<file>`; `home` is `/workspace`,
  `cwd` the chat's folder (created on first use; requests without `X-Session-Id` use a
  `default` folder).
- Every incoming path is normalised and resolved inside the caller's user root; `..`,
  absolute host paths, NUL bytes and symlinks are refused. workbench never creates symlinks.
- Inputs are immutable: uploads cannot be overwritten by tools; outputs are new files (a
  name clash gets a suffix). The agent cannot delete; only the user (file browser) and the
  retention sweep can.

### Sandbox (owner: free Python; design: mechanism)

Requirements the mechanism must meet:

1. The executor container has `network_mode: none`, a read-only root, `no-new-privileges`,
   `cap_drop: [ALL]` plus only what the supervisor needs to switch uids (listed and
   justified in `docs/sandbox.md`), a `pids_limit`, and a memory limit of
   slots × per-job memory + overhead, so it can never exhaust the host.
2. Each job runs in a fresh process under the uid of its slot (a pool distinct from the
   API's uid), with no capabilities. It sees only a per-job directory holding the files of
   its own workspace and writes only to that job's `out/`. It can read no other workspace,
   the spool, the retention state or any credential.
3. Limits per job: wall time, CPU time, address space, file size, open files, processes; on
   expiry the job is killed and the model gets a short error.
4. Interpreter: isolated mode (`-I`), no writable site-packages, non-interactive plotting
   backend, a fixed library set (below). A pre-forked "zygote" with libraries imported is an
   allowed optimisation if each job is still a fresh fork that shares no state.
5. After a job, its directory is wiped; outputs move into the workspace owned by the API.

A copy-in / copy-out job directory per job satisfies (2) simply and is the recommended
mechanism. Residual risk: one kernel shared with the host. `docs/sandbox.md` states it and
names gVisor (`runtime: runsc`) as optional hardening for hosts that have it.

Library set (design; licences must be compatible with distributing an Apache-2.0 image, so
no GPL or AGPL): pandas, numpy, scipy, matplotlib, openpyxl, xlsxwriter, python-docx,
docxtpl (with Jinja2), python-pptx, pypdf or pdfplumber, odfpy, lxml, beautifulsoup4,
charset-normalizer. `.msg` needs a parser with a compatible licence (`extract-msg` is GPL)
or is left out of version 1; the builder decides and documents it.

## 7. Tools (version 1)

| Tool (`operationId`) | Reads | Writes | Audit `action` |
|---|---|---|---|
| upload (file browser or chat attachment; not a tool) | — | file in the workspace | `collect` |
| `list_files` | names, sizes, dates of the chat's folder | — | not recorded (own metadata) |
| `read_document` | one file; PDF (text layer only), DOCX, XLSX/CSV, PPTX, ODT/ODS, EML (MSG see §6), HTML, TXT/MD; paging by page, sheet, range | — | `query` |
| `search_files` | text or regex across the chat's files | — | `query` |
| `run_python` | the chat's files, read-only | only `out/` of the job | `combine` |
| `inspect_template` | a template: its content controls, form fields or `{{ }}` placeholders | — | `query` |
| `fill_template` | template + values, each value with a `source` | a new file | `modify` |
| `write_table` | values | a new XLSX/CSV | `collect` |
| `write_document` | values / Markdown | a new DOCX/MD | `collect` |
| `display_file` | one file | — | `disclose` |
| view, download, archive (file browser) | one file | — | `disclose` |
| delete (file browser) | — | removes a file | `delete` |

Rules for all tools:

- Results to the model are capped (default 16 KB) and say when they were cut and how to page
  on. `run_python` returns stdout/stderr (capped), the list of files it wrote, and images as
  files, not inline.
- `read_document` on a PDF reports `pages_without_text` and a hint that the file needs OCR
  elsewhere; it never returns an empty page as if it were blank.
- Templates (owner: these two kinds; design: how):
  - **Word content controls and legacy form fields** (`.docx`, `.dotx`). A field is named by
    the control's tag, else its title; a legacy form field by its bookmark. Plain and rich
    text, date, drop-down and check box controls are filled within their type (a drop-down
    only with one of its entries). A `.dotx` yields a `.docx`. The template's styles, headers
    and protection are kept.
  - **`{{ field }}` placeholders.** DOCX through docxtpl (Jinja: fields, loops, conditions);
    XLSX through `{{ field }}` cells for single values and a `{{ table:<name> }}` anchor cell
    that writes rows downwards for a table.
  - `inspect_template` lists every field with its kind, type, allowed entries and where it
    sits, so the model can plan the fill and the user can confirm it.
- `fill_template` sources (design): every value carries a `source` of one of four kinds:
  `file` (path plus page, sheet or cell), `job` (the `run_python` job that computed it),
  `user` (stated by the user in the chat), `assistant` (text the model wrote). workbench
  checks the structure (the file, page, sheet or cell and the job exist in this workspace),
  not that the value is really there: that would need a log of everything read, another
  store of derived data. The filled file gets a sources appendix (DOCX: a final table; XLSX:
  an extra sheet), on by default, that names each value's kind, so a reader sees at once
  what is evidenced and what the assistant wrote.
- No tool deletes, overwrites an input, or reaches outside the caller's user root.

## 8. The assistant's behaviour (served at `GET /system`)

The system prompt is workbench's, versioned with it. It tells the model to: take a task
down as a plan first (inputs, operations, output, template) and have the user confirm it
before running tools; cite the source of every figure; never invent content a tool did not
return; say when a PDF needs OCR; point to docint when the task is a project (a corpus,
weeks of work, collections); and mention that workspace files are deleted after the
retention period; and say, when a template has neither content controls, form fields nor
`{{ }}` placeholders, that version 1 cannot fill it and what it needs. The wording is the
builder's; German and English are both expected from users.

## 9. Audit, retention, limits

### Audit (deploy ADR 0003)

- One chain, written by `api`. Each tool call and each file-browser action that reads or
  changes content is one event: `actor.user` = the caller's user id, `actor.via =
  "service"`, the caller in `criteria`; `target` by reference (file ids, never names typed
  by users; the chat folder as `container`); `parent_event_id` links the events of one agent
  turn where the caller lets workbench tell them apart (Open WebUI: same chat and message
  window; the builder decides).
- File names stay out of every audit line (design). The file browser passes them in query
  strings (`/files/view?path=/workspace/<chat>/<name>`). The gateway's access log already
  cuts query strings (edge-plane `caddy/Caddyfile`), but Open WebUI's own audit records keep
  the full URL (`utils/audit.py`: `request_uri=str(request.url)`) and go to the audit store
  for a year. open-webui-service therefore sets `AUDIT_EXCLUDED_PATHS: "/terminals"`
  (upstream matches it as a prefix of `/api/v1/terminals`); workbench records those
  accesses itself, by file id. The auditing of chat routes is untouched.
- No purpose in version 1: Open WebUI cannot send one per call. The limitation is stated in
  `docs/audit.md`. A caller that sends `X-Audit-Purpose` (docint, version 2) has it recorded.
- A failed audit write stops the operation (fail closed); a deletion that cannot be recorded
  does not happen.

### Retention (owner: 7 days)

- Every file — upload or output — is deleted `WORKBENCH_RETENTION_DAYS` (default 7) after it
  was created, whether or not the chat still exists. Empty chat folders go with their last
  file.
- The sweep runs in `api` at start and then periodically, records each batch before
  deleting it, and never deletes without the record (the pattern of open-webui-service
  `retention/owui_retention.py`). `make retention-dry-run` prints counts and ages only.
- The period is announced: in the system prompt, and in open-webui-service's banner
  (version 1 integration).
- Copies outside workbench: tool results stay in the Open WebUI chat history until the
  chat's own retention (365 days). `docs/data-flow.md` says so.

### Limits and scaling (design; all set in `.env`)

| Setting | Default | Why |
|---|---|---|
| `WORKBENCH_EXECUTOR_SLOTS` | 4 | concurrent jobs; one uid per slot |
| `WORKBENCH_QUEUE_MAX` | 16 | beyond it the call answers "busy, retry later" at once |
| `WORKBENCH_QUEUE_WAIT_S` | 60 | longest wait for a slot |
| `WORKBENCH_JOBS_PER_USER` | 1 | fairness between users |
| `WORKBENCH_JOB_WALL_S` | 120 | per job; Open WebUI sets no tool-call timeout by default |
| `WORKBENCH_JOB_MEMORY_MB` | 1536 | per job; container limit = slots × this + overhead |
| `WORKBENCH_RESULT_MAX_KB` | 16 | text returned to the model |
| `WORKBENCH_UPLOAD_MAX_MB` | 50 | per file, streamed to disk |
| `WORKBENCH_WORKSPACE_MAX_MB` | 500 | per user; full disk answers `507` |

- The first bottleneck is inference, not workbench: an agent turn calls the model several
  times with a growing context, and vllm-service serves 8 sequences by default
  (`CHAT_MAX_NUM_SEQS`) with an 8192-token context (`CHAT_MAX_MODEL_LEN`). At 8192 tokens the
  system prompt, the tool schemas and one result already fill the window. Owner decision:
  `CHAT_MAX_MODEL_LEN=32768`, measured on the production GPU before it goes live
  (concurrency of short and long chats, `CHAT_GPU_MEMORY_UTILIZATION` if needed). vLLM's paged
  KV cache reserves no full window per sequence, so short chats stay parallel; long agent
  chats take more. **workbench version 1 is released only after that vllm-service change.**
- Open WebUI's tool calls have no timeout by default (`AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`
  unset); open-webui-service sets one above workbench's wall time plus queue wait as a
  backstop.
- `/metrics` (Prometheus text, counts only, no key) exposes queue length, waits, job
  durations, refusals and disk use; obs-plane scrapes it and alerts on disk.
- Later, more executor replicas can take jobs from the same queue; `api` stays one instance
  while workspaces live on a local volume.

## 10. Deliverables of version 1

In `workbench`:

- the `api` and `executor` services, their images, compose files (production shape, no host
  ports; dev overlay), Makefile targets including `network`, `volumes`, `retention-dry-run`,
  `nuke`, `bundle`;
- tests: unit tests per tool and per path rule; isolation tests (egress fails, another
  workspace is unreadable, limits kill a job, symlinks and traversal are refused); a load
  test with 10 synthetic users (queue, refusals, memory ceiling, latencies measured apart
  from inference); the contract test (§5); all data synthetic;
- docs: `CLAUDE.md`, `README.md`, `docs/configuration.md`, `docs/permissions.md` (the tool
  matrix of §7 in full: per tool what it reads, writes, records and keeps), `docs/data-flow.md`,
  `docs/audit.md` (federation shape), `docs/retention.md`, `docs/sandbox.md`, and a section
  "Facts for the record of processing and the impact assessment" (the legal assessment stays
  with the operator, ADR 0003).

Elsewhere, each in its own PR:

- deploy: ADR 0005 "workbench, the shared tool tier" (new member, new data store, fourth
  network seam, the new invariant); `APP_DIRS` and bring-up order.
- open-webui-service: join `workbench-net`; `TERMINAL_SERVER_CONNECTIONS` from `.env` (one
  bearer connection, `chat_uploads: "filesystem"`, access grant for all users);
  `ENABLE_TOOL_SERVERS=true` (tools and functions stay off); edge-guard refuses writes to
  `/api/v1/configs/tool_servers` and `/api/v1/configs/terminal_servers`, so only the
  `.env` connection exists; `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`;
  `AUDIT_EXCLUDED_PATHS: "/terminals"` (§9); the banner names the workspace period;
  CLAUDE.md.
- obs-plane: `("workbench", "api")` in `DEFAULT_EMITTERS`; join `workbench-net`; scrape
  `/metrics`; a disk alert.
- vllm-service: `CHAT_MAX_MODEL_LEN=32768`, measured on the production GPU (§9); it lands
  before workbench version 1 is released.
- infra `CLAUDE.md`: the twelfth project, the fourth seam, the new invariant.

## 11. Version 2 (outline, its own design round)

An MCP door (Streamable HTTP) generated from the same tool registry; `WORKBENCH_CLIENTS`
with a key and an allowed tool list per caller; docint as caller, adding workbench's tools
to its `ToolRegistry` (`docint/agents/turn/tools.py`), sending retrieval results as a
`disclose` with purpose and taking results back into its Report Builder; docint's backend
joins `workbench-net`. Open question for that round: one identity across callers (Open
WebUI's opaque id vs. docint's edge user name).

## 12. Left to the builder

The job handoff mechanism; the exact supervisor capabilities; the zygote; the contract-test
harness; how agent turns are grouped in the audit; the `.msg` decision; the system prompt's
wording; Python version and library pins; test layout; package names.
