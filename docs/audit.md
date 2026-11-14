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
`container`. The event is written before the content leaves or the file goes.
A new file is the exception: an upload, and a file a tool made, is recorded once
it is stored, because its file id exists only then; one whose event cannot be
written is removed again.

Every event a user's request causes has this shape (`workbench/api/audit.py:AuditLog.record`):

| Field | Value |
|---|---|
| `action` / `operation` | by event key, below |
| `actor` | `{"user": <X-User-Id>, "via": "service"}`: Open WebUI's opaque account id (see [Who](#who-the-actor)) |
| `outcome` | `ok`; `error` where the table says so |
| `target` | `kind: file`, `ids` (the files' ids, at most 200; more go on continuations), `count`, `container: chat:<chat-id>` |
| `criteria` | `caller` (`open-webui`), and the keys below |

The `container` is the chat folder the files live in, never the chat the
request named (`workbench/api/files.py:_in_folder_of`). Open WebUI's browser can
name any chat in `X-Session-Id`, so only the file's own folder says where it is.

### Every event key

`OPERATIONS` in `workbench/api/audit.py` lists them.

| Key | `action` / `operation` | `target.ids` | `criteria` besides `caller` | Outcome |
|---|---|---|---|---|
| `upload` | `collect` / `file.upload` | the new file | — | `ok`, once the file is stored |
| `view` | `disclose` / `file.view` | the file viewed or downloaded | — | `ok`, before the bytes go |
| `read` | `disclose` / `file.read` | the text file read whole | — | `ok`, before the text goes; a file that is not text or is too large is refused and not recorded |
| `display` | `disclose` / `file.display` | the file shown as a card | — | `ok`; a file that does not exist is not recorded |
| `delete` | `delete` / `file.delete` | the file deleted by the user | — | `ok`, before the file goes |
| `read_document` | `query` / `tool.read_document` | the file read | `paged`: whether the call asked for a part | `ok`, once the job has answered and before the text goes |
| `search_files` | `query` / `tool.search_files` | every file the search was given | `regex`: whether the query is a regular expression; `max_hits` | `ok`, once the job has answered and before the hits go |
| `run_python` | `combine` / `tool.run_python` | every file the job was given, then every file it made | `outputs`: how many files it made that were kept | `ok` when the job ran to an answer: the code may have raised (its traceback is the answer) or run out of its own time, 9/10 of the wall time (stderr then says it timed out); `error` when the job failed, was stopped at the wall time or on a resource limit, its outputs broke the executor's limits, or its answer could not be used |
| `inspect_template` | `query` / `tool.inspect_template` | the template | — | `ok`, once the job has answered and before the fields go |
| `fill_template` | `modify` / `tool.fill_template` | the template, every file a value's source names, and the new file | `filled_by_source`: how many fields each kind of source filled (`file`, `job`, `user`, `assistant`) | `ok`, once the new file is kept |
| `write_table` | `collect` / `tool.write_table` | the new file | `rows`, `columns`: the table's size | `ok`, once the new file is kept |
| `write_document` | `collect` / `tool.write_document` | the new file | — | `ok`, once the new file is kept |

An event about more than 200 files is followed by continuation events under
`<operation>.ids` (`tool.search_files.ids`, `tool.run_python.ids`,
`tool.fill_template.ids`), each naming up to 200 more of its files (see
[Long lists of files](#long-lists-of-files)).

`fill_template`, `write_table` and `write_document` also write an `error`
event in one case: the job's answer could not be used, so the new file must
go, and it could not be removed. That event names the files the job was given
and the file that stayed, with `outputs` counting the files that stayed
(`workbench/api/tools.py:_remove_unsent`).

[permissions.md](permissions.md#which-runs-are-audited) says which runs are
recorded and which are not.

### Not recorded

- Listing a folder (`/files/list`), `list_files`, `mkdir` and `move`: they read
  or change no content. Moving a file keeps its id, so its later events name the
  same file.
- The handshake routes (`/api/config`, `/system`, `/openapi.json`,
  `/files/cwd`, `/skills`), `/healthz` and `/metrics`.
- A request refused before it becomes an operation: no or a wrong key (`401`),
  a missing or invalid user (`400`), a body that does not validate (`422`), a
  full queue (`503`), a file that does not exist (`404`).
- A tool call that failed before its work was done, and a job whose answer
  never reached the api (see
  [permissions.md](permissions.md#which-runs-are-audited)).

### What is never recorded

No event holds a file's content, a file name or a path, a search query, code,
a value the model wrote, Markdown, a template's field names, or the text a tool
returned. The api's audit methods take file ids and counts and no name
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

The actor of every user's event is `X-User-Id`, Open WebUI's opaque account id,
with `via: service`: Open WebUI acts for the user. While the account exists, an
Open WebUI admin resolves the id in Open WebUI's user table, and Open WebUI's
own per-request audit records carry the same id with the account. The caller,
`open-webui`, is in every event's `criteria`.

The id is trusted because only a caller with the bearer key reaches the api
(`workbench/api/auth.py`). Open WebUI can sign a token naming the user
(`X-OpenWebUI-User-Jwt`). In one observed run of v0.13.3 it reached only the
per-chat handshake reads, neither the proxied calls nor the model's tool calls;
no test pins that (see
[permissions.md](permissions.md#what-open-webui-v0133-sends)). workbench does
not use it.

## No purpose and no recipient in version 1

- **No purpose.** Open WebUI cannot send a stated purpose with each call, so
  version 1 records none and requires none. workbench does not read
  `X-Audit-Purpose` or `AUDIT_REQUIRE_PURPOSE`. A caller that sends a purpose
  (docint, in version 2) will have it recorded.
- **No `recipient` on `disclose`.** A view, read or display goes to the user's
  own session, through the caller that asked; it is not a transfer to a third
  party, so the event names no recipient.
- **No request id, and no parent but a continuation's.** Open WebUI's tool calls
  carry no message id workbench could tie together. The events of one agent turn
  share their `container` and follow each other in the chain. Only a
  continuation of a long id list names a parent (see
  [Long lists of files](#long-lists-of-files)).

## The Open WebUI side

Open WebUI's built-in audit log records every request it serves, with the full
URL. The file browser's URLs carry file names in their query strings
(`/api/v1/terminals/<id>/files/view?path=/workspace/<chat>/<name>`).
`AUDIT_EXCLUDED_PATHS=/terminals`, which Open WebUI matches as a prefix of
`/api/v1/terminals`, keeps those URLs out of its audit records; workbench
records those accesses itself, by file id. open-webui-service must set it when
its workbench connection is added: its `main` sets the variable empty today, and
without the setting Open WebUI's audit records would carry the file names. The
contract test runs its own Open WebUI with the setting and checks that no record
names an uploaded file
(`tests/contract/test_open_webui_contract.py:test_open_webui_audit_skips_the_terminal_proxy`);
it does not check open-webui-service's value.

Open WebUI's access log is a separate stream and still carries those URLs (see
[data-flow.md](data-flow.md#what-leaves)).

## Fail closed

- An action whose event cannot be written does not happen. The request gets
  `500` with `{"detail": "audit unavailable"}` (`workbench/api/errors.py`). An
  upload is removed again; a tool's new files are removed again; a view, read
  or display sends nothing; a delete leaves the file. A continuation that cannot
  be written fails the action the same way; the events written before it stay.
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
  `upload_max_mb`, `workspace_max_mb`. Never the key;
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

`AUDIT_REQUIRE_PURPOSE`, `AUDIT_CASE_REF`, `AUDIT_REASON` and `AUDIT_CLI_USER`
are read by other members of the federation. workbench reads none of them.
