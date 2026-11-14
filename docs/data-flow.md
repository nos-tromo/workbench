# Data flow

What enters workbench, where it is stored, for how long, and where it goes
from there. The last section collects the facts an operator needs for the
record of processing and the impact assessment.

> **The main copy outside workbench is Open WebUI's chat history.** Every tool
> result workbench sends (text read from a document, search hits, what code
> printed, file names and paths) becomes part of the chat in Open WebUI, and so
> does every argument the model wrote (code, values, Markdown). Open WebUI keeps
> a chat for its own retention period, 365 days by default
> (open-webui-service, `OPEN_WEBUI_CHAT_RETENTION_DAYS`), far longer than
> workbench's 7 days. Deleting a file in workbench does not remove what a chat
> already holds of it.

## What enters

- **Uploads.** A user attaches a file to a chat, or drops it into the file
  browser. The browser sends it through Open WebUI's terminal proxy to
  `POST /files/upload`. With the connection's `chat_uploads: filesystem`, Open
  WebUI stores nothing of it and builds no vectors.
  workbench streams it to disk (`workbench/api/store.py:Store.add_upload`).
- **Tool arguments the model writes.** Paths, search queries, Python code,
  table values, Markdown, the values of a template fill and the name of a new
  file (`workbench/api/tools*.py`). They reach the executor in the job's request
  and are not stored past the job.
- **Template values with their sources.** Each value of `fill_template` names
  where it came from: a file (with page, sheet or cell), a `run_python` job, the
  user, or the assistant. The api checks that a named file or job exists in the
  chat (`workbench/api/tools_templates.py:_sources`); the filled file ends with
  a table of the sources.
- **Identity headers.** The caller key, `X-User-Id` (Open WebUI's opaque account
  id) and `X-Session-Id` (the chat id). See
  [permissions.md](permissions.md#the-boundaries).

## Where it is stored

Everything lives on the external volume `workbench-data`, mounted at `/data`.
`workbench.executor.prepare_volume` lays it out before the services start
(names in `workbench/layout.py`):

| Path | Owner, mode | Holds |
|---|---|---|
| `/data/` | root, group of the api, `1771` | the parts below; the api may add the database's journal files here |
| `workspaces/<caller>/<user-id>/<chat-id>/…` | api, `0700` | the users' files: uploads and tool outputs, as plain bytes under the names users chose |
| `staging/<job-id>/<name>` | api, `0700` | outputs the executor hands over; each is moved into its chat folder within the same request |
| `jobs/<job-id>/` | `jobs/` root's, `0711`; each job's folder its slot's, `0700` | one private folder per running job: copies of its input files, its `out/`, its `tmp/`, the request (`spec.json`) and the answer (`result.json`); removed when the job ends |
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
| A job's folder in the executor | until the job ends | `workbench/executor/supervisor.py:Supervisor._remove` |
| Staging residue (outputs nobody registered) and orphans (files with no row, such as an interrupted upload) | one hour after they were last changed | `Sweeper._prune` |
| Empty folders, empty chat folders and empty user roots | one hour after they were last changed | `workbench/api/store.py:Store.sweep_user` |

[retention.md](retention.md) has the rules in full.

## What leaves

- **Tool results go to Open WebUI**, the caller that asked. Open WebUI puts
  them into the chat and sends them to the model through the federation's
  inference router. They stay in the chat history for Open WebUI's chat
  retention, 365 days by default (see the box at the top).
- **Files go to the user's browser** through Open WebUI's proxy, when the user
  views or downloads them (`GET /files/view`), or when a card shows one
  (`display_file`). What a user downloads is outside workbench from then on.
- **Audit events** go to the api container's standard output. obs-plane
  collects them into its audit store (see [audit.md](audit.md)). They name
  files by id, never by name.
- **Operational log lines** go to standard error. workbench's own lines carry
  counts, fixed texts and the types of errors: never a path, a file name, a user
  id, the message of an unexpected error or a traceback. The expected errors are
  answered without a log line
  (`workbench/api/errors.py`). An error no handler expected is answered `500`
  `{"detail": "internal error"}` and logged as one line with the method, the
  route's template and the error's type, and the server never sees it
  (`workbench/api/errors.py:QuietErrors`). The executor's supervisor and the
  one-shot that lays out the volume do the same: one line with the type
  (`workbench/executor/supervisor.py:main`, `Supervisor._handle`,
  `workbench/executor/prepare_volume.py:main`). The api runs without an access
  log (`--no-access-log` in `docker/Dockerfile.api`).
- **Metrics.** `GET /metrics` gives queue lengths, waits, job durations,
  refusals by reason and disk use; no user id, chat id or file name
  (`workbench/api/metrics.py`).
- **Open WebUI's own access log carries file names.** Open WebUI's server logs
  every request it serves, the proxied terminal URLs with their query strings
  among them (`/api/v1/terminals/<id>/files/view?path=/workspace/<chat>/<name>`).
  So file names reach that container's standard output and, from there,
  obs-plane, and obs-plane's log retention applies to those lines. Open WebUI's
  audit records do not carry them: open-webui-service sets
  `AUDIT_EXCLUDED_PATHS=/terminals`, which the contract test pins. The access
  log is a current fact. workbench cannot change it, since those paths are Open
  WebUI's protocol; the fix belongs in open-webui-service or obs-plane.

Nothing else leaves. The executor has no network, and the api calls no model
and no other service.

## Facts for the record of processing and the impact assessment

These are facts about the software. The legal assessment stays with the
operator (deploy ADR 0003, "Consequences").

**Categories of data.** Whatever users upload, which workbench cannot know in
advance: documents, spreadsheets, e-mails, presentations, and what is in them.
That can include special categories of personal data. Besides: file names, the
Open WebUI account id of each user, chat ids, and the time and tool of each run.

**Categories of data subjects.** The users of Open WebUI, and every person named
in the files they upload or in the values the model writes.

**Recipients.** None beyond the caller. workbench answers only the caller that
asked, Open WebUI, for the same user. What Open WebUI does with an answer (the
chat history, the model call through the inference router) is Open WebUI's
processing, recorded there.

**Storage periods.** Files and job records: `WORKBENCH_RETENTION_DAYS`, 7 days by
default, from creation. Leftovers of interrupted work: one hour. Audit events:
obs-plane's audit retention (`AUDIT_RETENTION`, one year by default). Copies in
Open WebUI's chat history: its chat retention, 365 days by default.

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
- the outcome of the impact assessment;
- the notice to users: what workbench keeps and for how long, and that Open
  WebUI's chat history keeps tool results for longer;
- whether the retention periods (workbench's, Open WebUI's, the audit store's)
  fit the rules that apply;
- what to do about Open WebUI's access log carrying file names, until it is
  changed.
