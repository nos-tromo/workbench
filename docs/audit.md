# Audit log

workbench writes a data-protection audit log (deploy ADR 0003): one
hash-chained JSON event per audited operation on the api's standard output,
which obs-plane routes to its audit store. This page covers what workbench
records and the settings that shape it. The store, its retention and who may
read it belong to obs-plane: see its
[`docs/audit.md`](https://github.com/nos-tromo/obs-plane/blob/main/docs/audit.md).

## What is recorded

Every read, change, disclosure and deletion of a file's content is one event.
Each names its files by id, never by name, and the chat folder they are in as
`container`. The event is written before the content leaves, before the file
goes, and before a new file becomes visible:

- an upload gets its id before its first byte is written (the bytes go to
  `<id>.part`), and its event names that id before the rename makes it a file of
  the chat (`workbench/api/store.py:Store.add_upload`);
- a file a tool made gets its id once the job has answered, and the tool's event
  names it before the file is moved into the chat
  (`workbench/api/jobs.py:JobQueue`, its `Keeper`).

A new file whose event cannot be written is never stored. One that could not be
stored after its event, whatever the reason (a full disk, the user's quota, an
invalid name, a failed commit or an error the store did not foresee), or that
was stored and removed again because the call then failed, is recorded again
with `outcome: error`. A process that ends between the two leaves a recorded
`<id>.part`, which retention removes and records by that id.

Every event a user's request causes has this shape (`workbench/api/audit.py:AuditLog.record`):

| Field | Value |
|---|---|
| `action` / `operation` | by event key, below |
| `actor` | `{"user": <X-User-Id>, "via": "service"}`: a keyed pseudonym of docint's or Ghostwriter's user (see [Who](#who-the-actor)) |
| `outcome` | `ok`; `error` where the table says so |
| `target` | `kind: file`, `ids` (the files' ids, at most 200; more go on continuations), `count`, `container: chat:<chat-id>` |
| `criteria` | `caller` (`docint` or `ghostwriter`), and the keys below |

The `container` is the chat folder the files live in.

### Every event key

`OPERATIONS` in `workbench/api/audit.py` lists them.

| Key | `action` / `operation` | `target.ids` | `criteria` besides `caller` | Outcome |
|---|---|---|---|---|
| `upload` | `collect` / `file.upload` | the new file (Ghostwriter's answer names it as `id`) | — | `ok`, before the file is visible; `error` again when it could not be stored after that |
| `view` | `disclose` / `file.view` | the file viewed or downloaded | — | `ok`, before the bytes go |
| `chat_delete` | `delete` / `chat.delete` | every file of the chat Ghostwriter deletes, `container` that chat | — | `ok`, before anything of the chat goes, also with no file; `error` again when the deletion then failed |
| `read_document` | `query` / `tool.read_document` | the file read | `paged`: whether the call asked for a part | once the job has answered, before anything of the answer goes: `ok`, or `error` (below) |
| `search_files` | `query` / `tool.search_files` | every file the search was given | `regex`: whether the query is a regular expression; `max_hits` | once the job has answered, before anything of the answer goes: `ok`, or `error` (below) |
| `run_python` | `combine` / `tool.run_python` | every file the job was given, then every file it made that is kept | `outputs`: how many files it made that are kept | before any file it made is visible and before the answer goes. `ok` when the job ran to an answer: the code may have raised (its traceback is the answer) or run out of its own time, 9/10 of the wall time (stderr then says it timed out); `error` when the job failed, was stopped at the wall time or on a resource limit, its outputs broke the executor's limits, or its answer could not be used |
| `run_python_inline` | `combine` / `tool.run_python.inline` | none: `target.kind` is `inline_data`, `count` the data entries, `container` `chat:<session>` | `job`, `input_bytes`, `outputs` (returned), `dropped`; `refused` on a refusal | once the job has answered, before anything of the answer goes: `ok`, or `error`; `denied`, without a target, for a missing required purpose |
| `inspect_template` | `query` / `tool.inspect_template` | the template | — | once the job has answered, before anything of the answer goes: `ok`, or `error` (below) |
| `fill_template` | `modify` / `tool.fill_template` | the template, every file a value's source names, and the new file | `filled_by_source`: how many fields each kind of source filled (`file`, `job`, `user`, `assistant`); `outputs: 0` on an `error` | before the new file is visible: `ok`, or `error` (below), which keeps no file |
| `write_table` | `collect` / `tool.write_table` | the new file | `rows`, `columns`: the table's size; `outputs: 0` on an `error` | before the new file is visible: `ok`, or `error` (below), which keeps no file |
| `write_document` | `collect` / `tool.write_document` | the new file | `outputs: 0` on an `error` | before the new file is visible: `ok`, or `error` (below), which keeps no file |

An event about more than 200 files is followed by continuation events under
`<operation>.ids` (`tool.search_files.ids`, `tool.run_python.ids`,
`tool.fill_template.ids`), each naming up to 200 more of its files (see
[Long lists of files](#long-lists-of-files)).

Every tool job whose answer reached the api is recorded, whatever the answer
(`workbench/api/tools.py:record_answer`, `OutputKeeper`). It is `error` when the
job did not end in an answer the api sends on: it refused, ran out of time or
memory, failed, or answered something that could not be read or used. A refusal
adds `refused: <code>`, the runner's fixed word (`no_such_part`,
`invalid_template`, `invalid_value`, `bad_regex`…, or one of the executor's
own: `storage_full` when the data volume is below its free-space floor,
`hidden_memory` when the container held shared memory no job's count showed
and every running job was stopped), never its message. The
message goes to the model and can quote the file: a workbook's sheet names, a
template's line, a drop-down's entries, a source's sheets and range. Since it
discloses content, it is recorded like a read. If the event cannot be written,
the answer is `audit unavailable`, never the refusal.

A tool that makes files writes a second, `error` event, with `outputs` counting
them, for new files its event named that did not end as it said: not stored
after all (the user's quota, a full disk, an invalid name, any other error of
the store), or stored and then removed again, or not removed, when the call
failed or its caller went away (`workbench/api/jobs.py:JobQueue`).

[permissions.md](permissions.md#which-runs-are-audited) says which runs are
recorded and which are not.

### The inline event

Written before the answer goes out, the event can, in a rare cancellation, name
outputs that never reached docint, whose own `disclose` records whether an
answer came. An answer that cannot be read records `outputs` 0 and every output
as `dropped`.

### Not recorded

- Listing a folder (`/files/list`) and `list_files`: they read or change no
  content.
- `/healthz` and `/metrics`.
- `make nuke`, which removes the whole data volume: the operator's act on the
  host, outside the api and its chain ([retention.md](retention.md#make-nuke-is-outside-the-trail)).
- A request refused before it becomes an operation: no or a wrong key (`401`),
  a body too large (`413`), a missing or invalid user (`400`), a body that does
  not validate (`422`), a full queue (`queue_full`), a file that does not exist
  (`404`).
- A tool call refused before its job ran (an invalid name, a source that is not
  in the chat), and a job whose answer never reached the api (see
  [permissions.md](permissions.md#which-runs-are-audited)).

### What is never recorded

No event holds a file's content, a file name or a path, a search query, code,
a value the model wrote, Markdown, a template's field names, the text a tool
returned, the data a call brings, or a digest of it, or the message of a refusal
(only its code). The api's audit methods take file ids and counts and no name
(`workbench/api/audit.py`). The criteria are flags and counts.

### Long lists of files

An event names at most 200 file ids (`MAX_IDS` of the audit library). An action
on more files is split, never cut (ADR 0003):

- its event, under its own operation, names the first 200 ids, and
  `target.count` holds the full number;
- each further 200 ids go on a continuation event, `<operation>.ids` (for
  example `tool.search_files.ids`), with `parent_event_id` the event's
  `event_id`. It has the same `action`, `actor`, `outcome`, purpose and
  `container`, `target.count` the number of its own ids, and no `criteria`.

No event sets `ids_truncated`. `search_files` and `run_python`, which can be
given up to 1000 files, and `fill_template` with many file sources can need
continuations (`workbench/api/audit.py:AuditLog._emit`, with the shared
`audit_runtime.id_chunks`). Retention never does: its batches are of at most
200 files.

## Retention events

The retention sweep in the api records each deletion before it happens, as the
system (`workbench/api/retention.py:Sweeper`):

| Field | Value |
|---|---|
| `action` / `operation` | `delete` / `retention.sweep` |
| `actor` | `{"user": null, "via": "system"}` |
| `outcome` | `ok` before the files go; `error` for files that could not be removed |

What `criteria` and `target` hold depends on what is removed:

| Removed | `criteria` | `target` |
|---|---|---|
| Files past the period, one batch of one user | `period_days`, `cutoff` (the moment before which files are due, ISO 8601), `caller`, `owner` (the user id), `count`; no `rule` | `ids` of the batch, at most 200; `count` |
| A job's staging residue: outputs nobody registered, older than an hour | `rule: staging`, `job` (the job's id), `count` of its regular files, and `caller` and `owner` while the job's record exists | `count`, no ids; `container: chat:<chat-id>` while the job's record exists |
| Orphans of one chat folder: files with no row, older than an hour | `rule: orphan`, `caller`, `owner`, `count` | `ids`: the file ids the names of interrupted uploads carry (`<id>.part`), never a name; `count`; `container: chat:<chat-id>` |

Files that could not be removed are recorded again with `outcome: error` and
the same criteria, `count` being how many stayed. They keep their rows and the
next sweep tries again.

A retention record that cannot be written stops the api (see
[Fail closed](#fail-closed)). Nothing more is deleted until it can record again.
[retention.md](retention.md) has the rules.

## Who: the actor

The actor of every user's event is `X-User-Id`, a keyed pseudonym of docint's
or Ghostwriter's user, with `via: service`: the caller acts for the user. A
docint pseudonym resolves through docint's own `disclose` event for the call
(same request id and job), which names the user. A Ghostwriter pseudonym is an
HMAC keyed with Ghostwriter's own workbench key, so it resolves only in
Ghostwriter's own audit, through the event with the same request id. The
caller, `docint` or `ghostwriter`, is in every event's `criteria`.

The id is trusted because only a caller with the bearer key reaches the api
(`workbench/api/auth.py`).

## Purpose, recipient and request id

- **Purpose.** On the MCP door, and on Ghostwriter's file routes, a call states
  its purpose in `X-Audit-Purpose`, and it is recorded. `AUDIT_REQUIRE_PURPOSE`
  (`query,disclose,combine,modify,collect` in compose) makes a call without one
  `denied`, with no target (`428` on a file route).
- **No `recipient` on `disclose`.** A view goes to the user's own
  session, through the caller that asked; it is not a transfer to a third
  party, so the event names no recipient.
- **Request id.** The `X-Request-ID` of an MCP call, or of a Ghostwriter file
  route, is on its event, which ties it to the caller's own event (docint's
  `disclose`, Ghostwriter's `disclose` / `chat.tool`). Only a continuation of a
  long id list names a parent (see [Long lists of files](#long-lists-of-files)).

## Fail closed

- An action whose event cannot be written does not happen. The request gets
  `500` with `{"detail": "audit unavailable"}` (`workbench/api/errors.py`). An
  upload or a tool's new file is never stored; a view or tool answer, a
  refusal included, sends nothing; a chat delete leaves the chat. A
  continuation that cannot be written fails the action the same way; the events
  written before it stay.
- When a second, `error` event cannot be written after a store or a removal
  failed, the failure it was about decides the answer; the failed record is
  logged by its type.
- A retention record that cannot be written ends the api process
  (`os._exit(1)`) before anything more is deleted. stderr names how many files
  the record was about; `restart: unless-stopped` brings the api back. The
  other way round is allowed: a record whose files were not all removed (the
  api stopped meanwhile) is followed by a second record when the next sweep
  removes them.
- If the chain cannot be opened at start (an unknown `AUDIT_OUTPUT`, a stream
  that cannot be opened, a first event that cannot be written), the api does not
  start (`workbench/api/main.py`: `app_factory` opens the stream, the lifespan
  of `create_app` writes the first events).

## The chain

The api runs one process (`--workers 1`), which writes one chain, opened by
the FastAPI lifespan, never at import. Its events carry `service: workbench`,
the compose project name the audit store binds the chain to, and the release
as `service_version`:

- `lifecycle` / `start` opens it;
- `admin` / `config.audit` (actor `via: system`) records the limits in force:
  `retention_days`, `executor_slots`, `queue_max`, `job_wall_s`,
  `upload_max_mb`, `workspace_max_mb`, `callers` (up to two, each by name,
  door, tools, wall cap and slots) and `require_purpose`, sorted. Never a key;
- `lifecycle` / `heartbeat` follows every 60 seconds;
- `lifecycle` / `shutdown` closes it on a clean stop.

obs-plane's verifier accepts chains only from the emitters it lists, by
compose project and service. workbench's is `workbench` / `api`, which
obs-plane's `DEFAULT_EMITTERS` gains in the same round as this release.

The event format and the hash chain come from
`workbench/utils/audit_event.py`; the stream from
`workbench/utils/audit_runtime.py`. Both are byte-identical copies of the
canonical files in `nos-tromo/.github` (`configs/audit/`) and are never edited
here; CI fails on drift. `workbench/api/audit.py` is workbench's own glue
around them.

## Where the lines go

stdout carries audit events only; operational log lines go to stderr, and the
api runs without an access log. With `AUDIT_OUTPUT=container`, which
`docker/compose.yaml` sets, events go to the container's main stdout
(`/proc/1/fd/1`) under a lock held by every audit writer in the container. The
lock file is `/tmp/audit-stream.lock` on the api's `/tmp` tmpfs.

## Settings

| Variable | Default | Purpose |
|---|---|---|
| `AUDIT_OUTPUT` | `stdout` (compose sets `container`) | Where the chain goes: `stdout` for the process's stdout, `container` for the container's main stdout under the container-wide lock. Any other value stops the start. Not in `.env.example`: compose fixes it |
| `AUDIT_REQUIRE_PURPOSE` | none in the code; compose sets `query,disclose,combine,modify,collect` | The actions a call must state a purpose for, on the MCP door and Ghostwriter's file routes ([configuration.md](configuration.md#audit-and-retention)) |

`AUDIT_CASE_REF`, `AUDIT_REASON` and `AUDIT_CLI_USER` are read by other members
of the federation. workbench reads none of them.
