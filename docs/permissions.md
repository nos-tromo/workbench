# Permissions

What each tool and each file route of workbench may read and write, what it
records in the audit log, and what it keeps. Then the boundaries that hold for
all of them, and what the model is told.

workbench serves two callers, docint and Ghostwriter (see [Callers and
doors](#callers-and-doors)). Both call the tools through `/mcp`. Ghostwriter's
file routes are not tools, so the model cannot call them.

## Callers and doors

| Caller | Door | Files | Tools | Wall cap | Running jobs |
|---|---|---|---|---|---|
| `docint` | mcp | inline | `DOCINT_WORKBENCH_TOOLS` (`run_python`) | `DOCINT_WORKBENCH_WALL_S` (60) | `DOCINT_WORKBENCH_SLOTS` (2) |
| `ghostwriter` | mcp, and four file routes | workspace | `GHOSTWRITER_WORKBENCH_TOOLS` (the nine) | `GHOSTWRITER_WORKBENCH_WALL_S` (120) | `GHOSTWRITER_WORKBENCH_SLOTS` (2) |

- The door is checked right after the key, before any body is read
  (`workbench/api/auth.py:KeyGate`). docint gets `404` on every path but
  `/mcp`, `/healthz` and `/metrics`. Every such `404` is
  `{"detail": "not found"}`.
- Ghostwriter's key reaches `/mcp` and four file routes: `/files/upload`,
  `/files/view`, `/files/list` and `/files/chat`. A purpose it must state and
  did not is `428` on a file route.
- Every path Ghostwriter names is confined to `X-Session-Id`'s chat, anything
  else being `404`; `default` or no session is `400`. For up to an hour after
  `DELETE /files/chat` (`DELETED_CHAT_HOLD_S`), an upload or a tool call for
  that chat is refused (`404` on a route).
- The tool list is enforced on `/mcp`: `tools/call` answers a tool not on the
  caller's list `400`, `-32602`, "unknown tool".
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
| `read_document` | one file, as a copy in a job | nothing | `query` / `tool.read_document` | a job record |
| `search_files` | the chat's newest 1000 files, as copies in a job; the executor never copies a file below the chat's `out/` folder, though the event names it | nothing | `query` / `tool.search_files` | a job record |
| `run_python` | every file of the chat, the newest 1000 outside `out/`, as read-only copies in a job | the job's `out/`: at most 20 files, which become new files of the chat; and its private `home/` and `tmp/`, removed with the job | `combine` / `tool.run_python` | the new files and a job record |
| `inspect_template` | one template or slide master, as a copy in a job | nothing | `query` / `tool.inspect_template` | a job record |
| `fill_template` | the template and every file a value's source names, as copies in a job | one new `.docx` or `.xlsx` in the template's chat; the template is not changed | `modify` / `tool.fill_template` | the new file and a job record |
| `write_table` | nothing of the chat: the values come in the call | one new `.xlsx` or `.csv` in the chat | `collect` / `tool.write_table` | the new file and a job record |
| `write_document` | nothing of the chat: the Markdown comes in the call | one new `.docx` or `.md` in the chat | `collect` / `tool.write_document` | the new file and a job record |
| `write_presentation` | the slide master (`.pptx` or `.potx`), every picture of the chat a slide shows (PNG, JPEG or GIF) and every file a slide's source names, as copies in a job | one new `.pptx` in the master's chat (a `.potx` master gives a `.pptx`); the master, the pictures and the sources are not changed | `modify` / `tool.write_presentation` | the new file and a job record |
| `run_python` (inline, docint) | the data of the call, as read-only files of a job | the job's `out/`, `home/` and `tmp/`, removed with the job; the outputs of the allowed types come back in the answer | `combine` / `tool.run_python.inline` | a job record; no file |

The nine tools answer Ghostwriter with `outputs` besides their own answer: the
id of each file the call wrote and, for a csv or xlsx file, a preview of it.

Sources in the code: `workbench/api/tools.py` (`list_files`,
`read_document`, `search_files`, `run_python`),
`workbench/api/tools_templates.py` (`inspect_template`, `fill_template`),
`workbench/api/tools_output.py` (`write_table`, `write_document`),
`workbench/api/tools_presentations.py` (`write_presentation`).

### File routes

Ghostwriter's routes (`workbench/api/files.py`), each held to the request's
chat.

| Route | Reads | Writes | Audit `action` / `operation` | Kept |
|---|---|---|---|---|
| upload, `POST /files/upload` | the uploaded bytes | one new file in the chat folder; a taken name gets a number | `collect` / `file.upload`, before the file is visible | the file |
| view and download, `GET /files/view` | one file's bytes, sent to the caller | nothing | `disclose` / `file.view` | nothing |
| `DELETE /files/chat` | nothing | the chat's files and rows, its job rows and its folder | `delete` / `chat.delete`, first | nothing |
| list, `GET /files/list` | names, types, sizes and dates of one folder | nothing | not recorded | nothing |

The service routes (`/healthz`, `/metrics`) read no file content and are not
recorded (`workbench/api/health.py`, `workbench/api/metrics.py`). Every other
path answers `404` to a caller with the key, and `401` to anyone without it.

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
- The chat is a folder under the user's root. `X-Session-Id` names it, and
  Ghostwriter is held to it: a path in another chat answers `404`, as a file
  that does not exist (`workbench/api/store.py:Store._locate`). So:
  - `list_files`, `search_files` and `run_python` see only the request's chat;
  - `fill_template` and `write_presentation` take file sources only from the
    template's or master's chat, and `write_presentation` its pictures too.
- Files exist only inside chat folders. A file directly under `/workspace`
  cannot be made there, and `/workspace` and a chat folder are never the
  target of a file operation (`workbench/api/store.py:Store._file_target`).
- Uploads are never overwritten by a tool. Every tool result is a new file, and
  a taken name gets a number (`workbench/api/store.py:Store.register_output`).

### No network

The executor, where every file is opened, has no network at all
(`network_mode: none` in `docker/compose.yaml`), and a job has no socket at all.
A job sees only copies of its own chat's files, read-only (a docint job, only
the data its call brings), and can write only into its own `out/` folder and its
private `home/` (with its `tmp/`); both are removed with the job, and only what
is in `out/` can become a file of the workspace (see [sandbox.md](sandbox.md)).
The api calls no model and joins only `workbench-net`.

### The agent deletes nothing

No tool deletes or changes a file. Files go only when Ghostwriter deletes a
chat for its user (`DELETE /files/chat`), or by retention after the period
(`workbench/api/retention.py:Sweeper`).

## Which runs are audited

- Every tool job whose answer reached the api is recorded before anything of
  that answer goes out: `ok` for an answer the api sends on, `error` for any
  other (`workbench/api/tools.py:record_answer`, `OutputKeeper`).
- A refusal is recorded too, as `error` with `refused: <code>`. Its message
  goes to the model and can quote the file: a workbook's sheet names, a line of
  a template, a drop-down's entries, the sheets and range of a value's source,
  the layouts of a slide master. The event holds the code alone, and if it
  cannot be written the model gets `audit unavailable`, never the refusal.
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

The tool descriptions and the hints in a result tell the model, among other
things, the limits that matter to users:

- Only this chat's files are available to `run_python`. Code that looks for a
  file of another chat gets a hint saying so
  (`workbench/executor/runners/python.py:run_python`).
- Use threads, not multiprocessing. `multiprocessing` and process pools do not
  work in a job (see [sandbox.md](sandbox.md#limits-of-run_python)).
- For people who open a table in Excel, `write_table` writes `xlsx`. Excel reads
  a CSV file by the computer's regional settings: German Excel splits it on `;`
  and may read `1.5` as text or a date. CSV, comma-separated unless the call
  asks for `;` or a tab, is for other programs.
- A data file (JSON, CSV, XLSX) becomes a table file through `run_python`,
  which loads it in code and writes `out/` as `.xlsx` or `.csv`; its rows are
  not copied into `write_table`. `read_document` shows a JSON file indented,
  one value per line, and JSON Lines (`.jsonl`, `.ndjson`) line by line.
- A Word table holds at most 63 columns. `write_document` refuses a wider
  Markdown table (`refused`), with a text that points to `write_table`.
- A deck holds 1 to 30 slides and at most 6 blocks a slide, one for each place
  of its layout. A text block holds at most 2000 characters, a title 300, a
  subtitle 500 and the speaker notes 5000. A table holds at most 30 rows (the
  header counts), 10 columns and 500 characters a cell; a chart at most 50
  categories and 10 series, a value or null for each category, and a pie one
  series of values that are not negative. A deck shows at most 20 pictures (PNG,
  JPEG or GIF files of the chat, each at most 10 MiB and 40 megapixels, 30 MiB
  together) and a slide cites at most 20 sources. The api checks every bound
  before it sends a job, so a deck over one is a `422` that names the slide and
  the block, with no job and no event, and the runner checks them again
  (`workbench/decks.py`). Text that may not fit its place is shrunk to 60 % at
  most, and a warning names the slide when that does not do (the answer's
  `warnings`).
- A slide master's example slides never reach the new deck. They are removed,
  with the custom shows and sections that name them and the thumbnail and
  `docProps/app.xml` that show or list them; the deck takes the master's theme,
  layouts and media, none of the parts that name who edited or commented on the
  master, and of its core properties only the language. A master whose layouts
  or master slide link to its own slides is refused (`invalid_template`), a
  macro-enabled presentation or a slide show too (`unsupported_template`), and
  `fill_template` refuses a slide master and names `write_presentation`.
- Other limits: at most 20 new files per `run_python` call; a job is given the
  newest 1000 files of the chat; answers are cut at `WORKBENCH_RESULT_MAX_KB`
  and say how to go on; `read_document` does no OCR and lists pages without a
  text layer; Outlook `.msg` files cannot be read.
