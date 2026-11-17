# Data flow

What enters workbench, where it is stored, for how long, and where it goes
from there. The last section collects the facts an operator needs for the
record of processing and the impact assessment.

> **The main copy outside workbench is Open WebUI's chat history.** Every tool
> result workbench sends to Open WebUI (text read from a document, search hits,
> what code printed, file names and paths) becomes part of the chat in Open WebUI,
> and so does every argument the model wrote (code, values, Markdown). Open WebUI
> keeps a chat for its own retention period, 365 days by default
> (open-webui-service, `OPEN_WEBUI_CHAT_RETENTION_DAYS`), far longer than
> workbench's 7 days. Deleting a file in workbench does not remove what a chat
> already holds of it. docint and Ghostwriter, the other callers, keep what a run
> returns with the chat turn, each for its own retention.

## What enters

- **Uploads.** A user attaches a file to a chat, or drops it into the file
  browser. The browser sends it through Open WebUI's terminal proxy to
  `POST /files/upload`. With the connection's `chat_uploads: filesystem`, Open
  WebUI stores nothing of it and builds no vectors.
  workbench streams it to disk (`workbench/api/store.py:Store.add_upload`).
  This is the path of a chat that has Workbench selected as its terminal. From
  open-webui-service v0.14.0, an attachment in a chat without it goes to Open
  WebUI's own file routes, which its edge-guard refuses with `403` for every
  account. A temporary chat is the exception: there the browser extracts the
  text of a PDF or text file itself, or keeps a pasted image as a data URL, and
  sends it to the model inline with the message, like pasted text. It does not
  pass workbench, which records nothing of it (see
  open-webui-service's `CLAUDE.md`, § Workbench).
- **Tool arguments the model writes** (`workbench/api/tools*.py`). They reach
  the executor in the job's request. What becomes of them:
  - paths, search queries and Python code end with the job: workbench keeps
    none of them past it;
  - table values (`write_table`), Markdown (`write_document`) and the values of
    a template fill (`fill_template`) become the content of a new file, and
    the name the model gives a new file (`name`, `output_name`, or a file
    `run_python` writes into `out/`) becomes that file's name in the database
    and on the volume. Both are kept like every other file, for
    `WORKBENCH_RETENTION_DAYS` (`workbench/api/store.py:Store.register_output`).
- **Template values with their sources.** Each value of `fill_template` names
  where it came from: a file (with page, sheet or cell), a `run_python` job, the
  user, or the assistant. The api checks that a named file or job exists in the
  chat (`workbench/api/tools_templates.py:_sources`). By default the filled file
  ends with a table of the sources; the call can leave it out
  (`include_sources: false`).
- **docint's data** (`workbench/api/tools_inline.py`). The model's code and up to
  8 texts docint builds from the turn (passages of its documents, keyword
  checks, topic listings), which can hold special categories of data, with the
  user and session pseudonyms, the purpose and the request id in the headers.
  The data ends with the job (see
  [What a docint call leaves](#what-a-docint-call-leaves)).
- **Identity headers.** The caller key, `X-User-Id` (Open WebUI's opaque account
  id, or docint's or Ghostwriter's pseudonym) and `X-Session-Id` (the chat id, or
  docint's or Ghostwriter's session pseudonym). See
  [permissions.md](permissions.md#the-boundaries).

## Where it is stored

Everything lives on the external volume `workbench-data`, mounted at `/data`.
`workbench.executor.prepare_volume` lays it out before the services start
(names in `workbench/layout.py`):

| Path | Owner, mode | Holds |
|---|---|---|
| `/data/` | root, group of the api, `1771` | the parts below; the api may add the database's journal files here |
| `workspaces/<caller>/<user-id>/<chat-id>/…` | api, `0700` | the users' files: uploads under the names users chose, tool outputs under the names the model chose, as plain bytes |
| `staging/<job-id>/<name>` | api, `0700` | outputs the executor hands over; each is moved into its chat folder within the same request (an inline job's are read back and removed) |
| `jobs/<job-id>/` | `jobs/` root's, `0711`; each job's folder root's, `0710`, its group the slot's; its `home/` and `out/` the slot's, `0700` | one private folder per running job: copies of its input files, its `out/`, its `home/` with its `tmp/` and its answer (`result.json`), and the request (`spec.json`); removed when the job ends |
| `workbench.db` | api, `0600` | the SQLite database, in WAL mode with `secure_delete` on |

The database has two tables (`workbench/api/store.py`):

- `files`: per file its id, caller, user id, chat id, name (its path below the
  chat folder), size, media type, kind (`upload` or `output`), creation time
  and, for an output, the job that made it;
- `jobs`: per tool run its id, caller, user id, chat id, tool, start time and
  status. No argument, code or result.

**File names and user ids are personal data in themselves.** A file name can
name a person or a case, and the user id names an Open WebUI account. Both are
in the database and in the folder names on the volume.

Two more places hold data for a short time:

- the api's `/tmp`, a tmpfs in memory, where uploads are spooled while they
  arrive (at most the upload limit plus 1 MiB each);
- the database's `-wal` file, which can hold earlier versions of deleted rows
  until a checkpoint copies over them (see
  [retention.md](retention.md#after-a-sweep)).

workbench encrypts nothing at rest. Encryption of the host's disk is the
operator's.

## How long

| What | Kept for | Enforced by |
|---|---|---|
| Uploads and outputs | `WORKBENCH_RETENTION_DAYS` (7) days from creation; using, moving or renaming a file does not extend it, and there is no extension of any kind | `workbench/api/retention.py:Sweeper` |
| Job records | the same period, from the job's start | `Sweeper._forget_jobs` |
| A job's folder in the executor | until the job ends. One whose removal failed (an I/O error) is root's alone from then on and is tried again every five minutes until it goes, or until the executor restarts, which removes every job folder left | `workbench/executor/supervisor.py:Supervisor._remove`, `_retry_leftovers` |
| Staging residue (outputs nobody registered) and orphans (files with no row, such as an interrupted upload) | one hour after they were last changed | `Sweeper._prune` |
| Empty folders, empty chat folders and empty user roots | one hour after they were last changed | `workbench/api/store.py:Store.sweep_user` |

[retention.md](retention.md) has the rules in full.

## What a docint call leaves

docint's data comes with the call and its outputs go back in the answer; no
workspace and no file is made.

| Where | What | How long |
|---|---|---|
| the api's memory | the request, the data, the outputs | the request; freed memory is not cleared |
| the executor's memory | the request line with the data | the job; freed memory is not cleared |
| `jobs/<job-id>/` | the data as root's `0444` files; the job's `out/` and `home/` | the job |
| `staging/<job-id>/` | the outputs | until the job settles; retention removes a leftover after an hour |
| free blocks of `workbench-data` | deleted job files | until overwritten; encrypting the host's disk is the operator's |
| `workbench.db`, `jobs` | job id, caller, user pseudonym, session pseudonym, tool, status, times | `WORKBENCH_RETENTION_DAYS` |
| the audit chain | [the inline event](audit.md#every-event-key) | obs-plane's retention |

No core dump holds the data, [a host's handler aside](sandbox.md#residual-risks).

## What leaves

- **Tool results go to the caller that asked**: Open WebUI, Ghostwriter, or docint
  (workbench keeps neither docint's data nor its outputs). Open WebUI puts them into
  the chat and sends them, in the model's context, to whatever model the operator
  configured behind the federation's inference router. If that model is run by an
  outside provider (a hosted API, or an Ollama `*-cloud` model), the results leave
  the host and the federation to that provider. They stay in the chat history for
  Open WebUI's chat retention, 365 days by default (see the box at the top).
- **Files go to the user's browser** through Open WebUI's proxy, when the user
  views or downloads them (`GET /files/view`), or when a card shows one
  (`display_file`). What a user downloads is outside workbench from then on.
- **Audit events** go to the api container's standard output. obs-plane
  collects them into its audit store (see [audit.md](audit.md)). They name
  files by id, never by name.
- **Operational log lines** go to standard error. workbench's own lines carry
  counts, fixed texts and the types of errors: never a file name, a user id,
  the message of an unexpected error, or a path on the volume but the fixed
  ones of its layout. The expected errors are answered without a log line
  (`workbench/api/errors.py`). An error no handler expected while a request is
  served is answered `500` `{"detail": "internal error"}` and logged as one
  line with the method, the route's template and the error's type; the error
  itself never reaches the server, so no traceback is logged for it
  (`workbench/api/errors.py:QuietErrors`). If the answer had already started,
  it stops where it was, and uvicorn adds its own fixed line that the answer
  was not completed. The executor's supervisor and the one-shot that lays out
  the volume log such errors as one line with the type
  (`workbench/executor/supervisor.py:main`, `Supervisor._handle`,
  `workbench/executor/prepare_volume.py:main`). One kind of traceback remains:
  when the api cannot start or stop (the audit chain or the database cannot be
  opened), uvicorn logs the start-up or shutdown failure with its traceback. It
  happens outside any request and holds no user data, though it can name a
  fixed path such as `/data/workbench.db`. The api runs without an access log
  (`--no-access-log` in `docker/Dockerfile.api`).
- **Metrics.** `GET /metrics` gives queue lengths, waits, job durations,
  refusals by reason and disk use; no user id, chat id or file name
  (`workbench/api/metrics.py`).
- **Open WebUI's logs and the file names.** Open WebUI proxies the file
  browser's calls, and their URLs carry file names in their query strings
  (`/api/v1/terminals/<id>/files/view?path=/workspace/<chat>/<name>`). From
  open-webui-service v0.14.0 neither request log of Open WebUI prints them: its
  audit records skip the terminal proxy (`AUDIT_EXCLUDED_PATHS: "/terminals"`),
  and uvicorn's access log is off (`--no-access-log` together with
  `AUDIT_UVICORN_LOGGER_NAMES: ""`, both needed;
  [audit.md](audit.md#the-open-webui-side)). Still logging there: the audit
  records of Open WebUI's other routes, its application log (the proxy's errors
  name host and port, not the path), and the log of the edge-guard in front of
  it, which writes the decoded path without the query. The contract test runs
  its own Open WebUI with the audit setting and checks it; it does not check
  open-webui-service's values. workbench cannot change any of this, since those
  paths are Open WebUI's protocol; it belongs to open-webui-service. What the
  gateway logs is edge-plane's: its access log cuts the query string from the
  URI (`caddy/Caddyfile` there).

workbench itself sends nothing outside the host: the executor has no network,
and the api calls no model and no other service. What leaves the federation
leaves through the callers' model calls: Open WebUI's, above, and docint's and
Ghostwriter's.

## Facts for the record of processing and the impact assessment

These are facts about the software. The legal assessment stays with the
operator (deploy ADR 0003, "Consequences").

**Categories of data.** Whatever users upload (Ghostwriter's, by chat), or docint
sends with a call (passages of its documents), which workbench cannot know in
advance: documents, spreadsheets, e-mails, presentations, and what is in them.
That can include special categories of personal data. Also what the model
writes into new files (table values, Markdown, template values) and their
names. Besides: file names, the Open WebUI account id of each user, chat ids,
the user and session pseudonyms of docint and Ghostwriter with the purpose and
request id they send, and the time and tool of each run.

**Categories of data subjects.** The users of Open WebUI, docint and Ghostwriter
(the last two as pseudonyms), and every person named in the files or data they
send or in the values the model writes.

**Where data flows.** These are the flows, as facts:

- to the caller that asked, for the same user: tool results and files to Open
  WebUI; results to docint (workbench keeps neither its data nor its outputs);
  results to Ghostwriter, with `outputs` and a preview of each csv or xlsx file,
  which Ghostwriter keeps for `WORKBENCH_RETENTION_DAYS`;
- through Open WebUI, to the federation's inference router and the model the
  operator configured behind it: tool results enter the model's context. When
  that model is run by an outside provider, they leave the host and the
  federation to that provider. What Open WebUI does with an answer (the chat
  history, the model call) is its own processing, recorded there. An attachment
  in a temporary chat without Workbench selected takes the same way to the
  model, inline with the message, without passing workbench, which records
  nothing of it;
- through docint and Ghostwriter, to their models and their own databases: what
  a run returns enters their model context and is kept with the turn; their own
  processing, recorded there;
- to obs-plane: the audit events (account id, chat id, file ids, times, counts;
  the pseudonyms, purpose and request id of docint and Ghostwriter) into its
  audit store, and the containers' log lines into its log store. Metrics hold no
  personal data.

workbench itself sends nothing outside the host. Tool results go to whatever
model the operator configured behind the inference router; if that is an
outside provider, they leave the host and the federation. Whether any of these
is a recipient in the legal sense is for the operator to decide.

**Storage periods.** Files and job records: `WORKBENCH_RETENTION_DAYS`, 7 days by
default, from creation. Ghostwriter's chat delete (`DELETE /files/chat`) removes
the chat's files at once and leaves an event by file id, nothing else.
Leftovers of interrupted work: one hour. Audit events: obs-plane's audit
retention (`AUDIT_RETENTION`, one year by default). Copies in
Open WebUI's chat history: its chat retention, 365 days by default. Copies in
docint's session database, the analyses kept with the chat turn: docint's own
retention; workbench keeps neither the data nor the outputs of a docint call.
Docker's `local` log driver (`docker/compose.yaml`, `x-logging`) also keeps a
copy of each container's output on the host, the api's audit lines (account ids,
chat ids, file ids; the pseudonyms, purposes and request ids of docint and
Ghostwriter) among them: rotated by size, at most five files of 50 MB per
container, not by time, so with little traffic it can outlive obs-plane's
retention.

**Technical and organisational measures.**

| Measure | Where |
|---|---|
| One workspace per user; another user's file is not found | [permissions.md](permissions.md#the-boundaries) |
| A bearer key per caller; only callers and the scraper join `workbench-net` | [configuration.md](configuration.md#the-caller-key) |
| Every file opened in a sandbox without network, under a slot uid of its own | [sandbox.md](sandbox.md) |
| Hardened containers: read-only roots, no capabilities but those listed, no privilege escalation | [sandbox.md](sandbox.md#capabilities) |
| Every file read, change, disclosure and deletion recorded by file id, fail closed | [audit.md](audit.md) |
| Files deleted after the period, each deletion recorded first | [retention.md](retention.md) |
| No content, name or user id in workbench's own log lines or in metrics | [What leaves](#what-leaves) |
| No tool deletes or overwrites a file | [permissions.md](permissions.md#the-agent-deletes-nothing) |

**Decisions that stay with the operator.**

- the legal basis of the processing;
- which inference provider serves the chat model: one inside the federation,
  or an outside provider that receives the tool results;
- whether any of the flows above goes to a recipient in the legal sense;
- the outcome of the impact assessment;
- the notice to users: what workbench keeps and for how long, and that Open
  WebUI's chat history and docint's analyses keep tool results for longer;
- whether the retention periods (workbench's, Open WebUI's, docint's, the audit
  store's) fit the rules that apply;
- whether temporary chats stay available in Open WebUI, where an attachment in
  a chat without Workbench selected goes to the model without passing workbench
  (`USER_PERMISSIONS_CHAT_TEMPORARY`, which open-webui-service leaves at Open
  WebUI's default, on).
