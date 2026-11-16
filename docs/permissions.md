# Permissions

What each tool and each file route of workbench may read and write, what it
records in the audit log, and what it keeps. Then the boundaries that hold for
all of them, and what the model is told.

workbench serves three callers, Open WebUI, docint and Ghostwriter (see [Callers
and doors](#callers-and-doors)). Open WebUI turns every
operation of `GET /openapi.json` into a tool for the model
(`workbench/api/openapi.py:tool_openapi`). Its file browser calls the file
routes through Open WebUI's terminal proxy, `/api/v1/terminals/<id>/…`. The
file routes are not in the tool document, so the model cannot call them.

## Callers and doors

| Caller | Door | Files | Tools | Wall cap | Running jobs |
|---|---|---|---|---|---|
| `open-webui` | terminal | workspace | all nine | `WORKBENCH_JOB_WALL_S` | not capped |
| `docint` | mcp | inline | `DOCINT_WORKBENCH_TOOLS` (`run_python`) | `DOCINT_WORKBENCH_WALL_S` (60) | `DOCINT_WORKBENCH_SLOTS` (2) |
| `ghostwriter` | mcp, and four file routes | workspace | `GHOSTWRITER_WORKBENCH_TOOLS` (the eight, never `display_file`) | `GHOSTWRITER_WORKBENCH_WALL_S` (120) | `GHOSTWRITER_WORKBENCH_SLOTS` (2) |

- The door is checked right after the key, before any body is read
  (`workbench/api/auth.py:KeyGate`). An MCP caller gets `404` on every path but
  `/mcp`, `/healthz` and `/metrics`; a terminal caller gets `404` on `/mcp`,
  and so on `/api/v1/terminals/<id>/mcp` through Open WebUI's proxy. Every such
  `404` is `{"detail": "not found"}`.
- Ghostwriter's key reaches `/mcp` and four file routes: `/files/upload`,
  `/files/view`, `/files/list` and `/files/chat`. A purpose it must state and
  did not is `428` on a file route.
- Every path Ghostwriter names is confined to `X-Session-Id`'s chat, anything
  else being `404`; `default` or no session is `400`. For up to an hour after
  `DELETE /files/chat` (`DELETED_CHAT_HOLD_S`), an upload or a tool call for
  that chat is refused (`404` on a route).
- The tool list is enforced on both doors: a terminal tool route depends on
  `require_tool("<name>")`, which answers `404` for a tool not on the caller's
  list, and `tools/call` on `/mcp` answers it `400`, `-32602`, "unknown tool".
- A caller is a namespace: users are `(caller, user-id)`. docint has no folder
  under `workspaces/`.

## The matrix

"The chat" is the chat folder `/workspace/<chat-id>/` of the request
(`X-Session-Id`). "Kept" is what outlives the call. Every new file is a file of
the workspace and is deleted with the others (see [retention.md](retention.md)).
Every tool that runs in the executor also leaves a job record (see
[data-flow.md](data-flow.md#where-it-is-stored)).

### Tools

| Tool | Reads | Writes | Audit `action` / `operation` | Kept |
|---|---|---|---|---|
| `list_files` | names, sizes, dates and kinds of the chat's files, from the database | nothing | not recorded: the user's own metadata | nothing |
| `display_file` | the name and type of one file, from the database | nothing | `disclose` / `file.display`, when the file exists | nothing; the browser then fetches the bytes with `/files/view` |
| `read_document` | one file, as a copy in a job | nothing | `query` / `tool.read_document` | a job record |
| `search_files` | the chat's newest 1000 files, as copies in a job; the executor never copies a file below the chat's `out/` folder, though the event names it | nothing | `query` / `tool.search_files` | a job record |
| `run_python` | every file of the chat, the newest 1000 outside `out/`, as read-only copies in a job | the job's `out/`: at most 20 files, which become new files of the chat; and its private `home/` and `tmp/`, removed with the job | `combine` / `tool.run_python` | the new files and a job record |
| `inspect_template` | one template, as a copy in a job | nothing | `query` / `tool.inspect_template` | a job record |
| `fill_template` | the template and every file a value's source names, as copies in a job | one new `.docx` or `.xlsx` in the template's chat; the template is not changed | `modify` / `tool.fill_template` | the new file and a job record |
| `write_table` | nothing of the chat: the values come in the call | one new `.xlsx` or `.csv` in the chat | `collect` / `tool.write_table` | the new file and a job record |
| `write_document` | nothing of the chat: the Markdown comes in the call | one new `.docx` or `.md` in the chat | `collect` / `tool.write_document` | the new file and a job record |
| `run_python` (inline, docint) | the data of the call, as read-only files of a job | the job's `out/`, `home/` and `tmp/`, removed with the job; the outputs of the allowed types come back in the answer | `combine` / `tool.run_python.inline` | a job record; no file |

On `/mcp` the eight tools answer Ghostwriter as their routes do, plus `outputs`:
the id of each file the call wrote and, for a csv or xlsx file, a preview of it.

Sources in the code: `workbench/api/tools.py` (`list_files`,
`read_document`, `search_files`, `run_python`),
`workbench/api/tools_templates.py` (`inspect_template`, `fill_template`),
`workbench/api/tools_output.py` (`write_table`, `write_document`) and
`workbench/api/files.py:display_file`.

### File routes

The browser's routes (`workbench/api/files.py`).

| Route | Reads | Writes | Audit `action` / `operation` | Kept |
|---|---|---|---|---|
| upload, `POST /files/upload` | the uploaded bytes | one new file in a chat folder or a folder inside one; a taken name gets a number | `collect` / `file.upload`, before the file is visible | the file |
| view and download, `GET /files/view` | one file's bytes, sent to the browser | nothing | `disclose` / `file.view` | nothing |
| read as text, `GET /files/read` | one text file, sent whole | nothing | `disclose` / `file.read` | nothing |
| delete, `DELETE /files/delete` | nothing | removes one file and its row | `delete` / `file.delete`, before the file goes; again as `error` if it could not go | nothing |
| `DELETE /files/chat` (Ghostwriter only) | nothing | the chat's files and rows, its job rows and its folder | `delete` / `chat.delete`, first | nothing |
| mkdir, `POST /files/mkdir` | nothing | one folder, with its parents | not recorded: no content | the folder, until retention removes it empty |
| move, `POST /files/move` | nothing | a new name or folder for one file of the same user; nothing is overwritten | not recorded: no content | the file keeps its id and its creation time |
| list, `GET /files/list` | names, types, sizes and dates of one folder | nothing | not recorded | nothing |

The handshake routes (`/api/config`, `/system`, `/openapi.json`, `/files/cwd`,
`/skills`, `/skills/read`) and the service routes (`/healthz`, `/metrics`) read
no file content and are not recorded (`workbench/api/contract.py`,
`workbench/api/metrics.py`). Every other path answers `404` to a caller with the
key, and `401` to anyone without it.

[audit.md](audit.md) says what each event holds.

## The boundaries

### The user is the boundary; the chat is a folder

- A caller is known by its bearer key, compared in constant time
  (`workbench/api/auth.py:CallerRegistry.authenticate`). The key is checked
  before anything of a request's body is read, whatever its path: without it a
  request answers `401`, never `404`, `405` or `422` (`auth.py:KeyGate`), and
  `/healthz` and `/metrics` are the only routes that need none. The user is the
  caller's `X-User-Id`, trusted because only a caller with the key reaches the
  api (`workbench/api/auth.py:require_principal`). Every route that touches
  files needs it.
- The user's files live under `workspaces/<caller>/<user-id>/` on the data
  volume. Every path a request names is resolved below that root and nowhere
  else (`workbench/paths.py:Workspaces.resolve`). A path that leaves
  `/workspace` once normalised, a NUL byte, an invalid name and a symlink in
  the way are refused; workbench never makes a symlink.
- Another user's file answers `404`, the same answer as a file that does not
  exist (`workbench/api/errors.py`).
- The chat is a folder, not a boundary. `X-Session-Id` names it; a request
  without the header uses the chat `default`. Open WebUI's browser can send any
  chat id through the proxy. So:
  - `list_files`, `search_files` and `run_python` see only the request's chat;
  - the tools that take a path (`read_document`, `inspect_template`,
    `fill_template`, `display_file`) reach a file in any chat of the same user,
    and the event names the chat the file is in;
  - `fill_template` takes file sources only from the template's chat.
- Files exist only inside chat folders. A file directly under `/workspace`
  cannot be made, moved there or deleted there, and `/workspace` and a chat
  folder are never the target of a file operation
  (`workbench/api/store.py:Store._file_target`).
- Uploads are never overwritten by a tool. Every tool result is a new file, and
  a taken name gets a number (`workbench/api/store.py:Store.register_output`).

### What Open WebUI v0.13.3 sends

The contract test (`tests/contract/test_open_webui_contract.py`) runs the Open
WebUI image open-webui-service deploys against workbench and pins this:

- `X-User-Id` is always Open WebUI's opaque account id, on the browser's
  proxied calls and on the model's tool calls. Two accounts get two
  workspaces.
- `X-Session-Id` is the chat id, or the header is absent. It is never empty on
  the routes workbench serves: Open WebUI forwards it only when it has a value,
  and a request without it lands in `default`.
- A proxy call on the route `/api/v1/terminals/<id>/chats/<chat-id>/…` names
  its chat in the path, and that chat wins over the header.

One more fact comes from a single observed run of the same image, not from a
test: with user forwarding on, Open WebUI signs a token naming the user
(`X-OpenWebUI-User-Jwt`), and it sends it with the per-chat handshake reads
(`/files/cwd`, `/files/read` of `<home>/AGENTS.md`), but neither with the
proxied calls nor with the model's tool calls. workbench does not read it; the
actor of every event is the account id.

### What open-webui-service keeps in place

From open-webui-service v0.14.0, workbench is the only terminal server Open
WebUI offers. Its `docker/compose.yaml` sets the connection and turns Open
WebUI's own tools, functions and code execution off, and its nginx guard in front
of Open WebUI (`edge-guard`) refuses, for every account:

- writes to the tool-server, terminal-server and OAuth-client configuration and
  the configuration import, which would replace or add a connection;
- writes to `/api/v1/configs/connections` and `/api/v1/configs/code_execution`,
  which would switch on direct connections and direct tool-server integrations,
  and code execution and the code interpreter inside Open WebUI;
- every request under `/api/v1/tether`, through which a user could be offered a
  terminal of their own, outside workbench's sandbox, audit and retention.

`ENABLE_TOOL_SERVERS` follows the key: without `OPEN_WEBUI_WORKBENCH_KEY` there,
no tool or terminal server is on (open-webui-service's `CLAUDE.md`, § Workbench).

From open-webui-service v0.14.1 the connection has a chat context
(`server_type: orchestrator`, `contexts.chat.context_id: chat_id`): Open WebUI
holds an attachment of a chat not saved yet until the first message saves it,
so the file lands in that chat's folder, not in `default`, where the chat's
`list_files`, `search_files` and `run_python` would not see it. Calls with a
chat also carry `X-Terminal-Context-Id`, the chat id again; workbench does not
read it. A temporary chat is not offered workbench.

### No network

The executor, where every file is opened, has no network at all
(`network_mode: none` in `docker/compose.yaml`), and a job has no socket at all.
A job sees only copies of its own chat's files, read-only (a docint job, only
the data its call brings), and can write only into its own `out/` folder and its
private `home/` (with its `tmp/`); both are removed with the job, and only what
is in `out/` can become a file of the workspace (see [sandbox.md](sandbox.md)).
The api calls no model and joins only `workbench-net`.

### The agent deletes nothing

No tool deletes or changes a file. Files go only by the user's own delete in the
file browser, or by retention after the period
(`workbench/api/retention.py:Sweeper`).

## Which runs are audited

- Every tool job whose answer reached the api is recorded before anything of
  that answer goes out: `ok` for an answer the api sends on, `error` for any
  other (`workbench/api/tools.py:record_answer`, `OutputKeeper`).
- A refusal is recorded too, as `error` with `refused: <code>`. Its message
  goes to the model and can quote the file: a workbook's sheet names, a line of
  a template, a drop-down's entries, the sheets and range of a value's source.
  The event holds the code alone, and if it cannot be written the model gets
  `audit unavailable`, never the refusal.
- `run_python` names the files the job was given and the files it made; a fill
  or a write names its new file. Each is recorded before any file it made
  becomes visible. A new file the model is not told of (the answer could not be
  used) is never moved into the chat.
- A job whose answer never reached the api (the connection dropped, or the
  request was cancelled while the job ran) is not recorded as a tool call.
  Nothing of it reached the model, and the files it made are never kept
  (`workbench/api/jobs.py:JobQueue`). Its job record stays, until retention.
- What a call makes and removes within the same request is covered by that
  call's event: the job's folder in the executor. A new file named in the event
  that could not be stored after it (the user's quota, a full disk, an invalid
  name, any other error of the store), or that was stored and then had to go
  again because the call failed, whether it went or not, is recorded again with
  `outcome: error`. Only what outlives its request otherwise gets an event of
  its own, from retention.
- A new file whose event cannot be written is never stored, and the call fails
  (see [audit.md](audit.md#fail-closed)).

## What the model is told

The system prompt (`workbench/api/system_prompt.md`, served at `GET /system`)
and the tool descriptions tell the model, among other things, the limits that
matter to users:

- Files are deleted a fixed number of days after they were made; the prompt
  names the configured period, 7 days by default. The model is to tell the user
  to download what they want to keep, and to say so again with each result.
- Only this chat's files are available to `run_python`. Code that looks for a
  file of another chat gets a hint saying so
  (`workbench/executor/runners/python.py:run_python`).
- Use threads, not multiprocessing. `multiprocessing` and process pools do not
  work in a job (see [sandbox.md](sandbox.md#limits-of-run_python)).
- For people who open a table in Excel, `write_table` writes `xlsx`. Excel reads
  a CSV file by the computer's regional settings: German Excel splits it on `;`
  and may read `1.5` as text or a date. CSV, comma-separated unless the call
  asks for `;` or a tab, is for other programs.
- A Word table holds at most 63 columns. `write_document` refuses a wider
  Markdown table with a `422` that points to `write_table`.
- Other limits: at most 20 new files per `run_python` call; a job is given the
  newest 1000 files of the chat; answers are cut at `WORKBENCH_RESULT_MAX_KB`
  and say how to go on; `read_document` does no OCR and lists pages without a
  text layer; Outlook `.msg` files cannot be read.
