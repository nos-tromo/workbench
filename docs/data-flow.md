# Data flow

What enters workbench, where it is stored, for how long, and where it goes
from there. The last section collects the facts an operator needs for the
record of processing and the impact assessment.

> **The main copy outside workbench is the callers' chat history.** Every tool
> result workbench sends to Ghostwriter or docint (text read from a document,
> search hits, what code printed, file names and paths) becomes part of the chat
> turn the caller keeps, and so does every argument the model wrote (code,
> values, Markdown). Each keeps it for its own retention, which can be far
> longer than workbench's 7 days. Deleting a file in workbench does not remove
> what a chat already holds of it.

## What enters

- **Uploads.** A user drops a file into a Ghostwriter chat; Ghostwriter's
  backend sends it to `POST /files/upload`. workbench streams it to disk
  (`workbench/api/store.py:Store.add_upload`).
- **Tool arguments the model writes** (`workbench/api/tools*.py`). They reach
  the executor in the job's request. What becomes of them:
  - paths, search queries and Python code end with the job: workbench keeps
    none of them past it;
  - table values (`write_table`), Markdown (`write_document`), the values of a
    template fill (`fill_template`) and the slides of a deck
    (`write_presentation`: titles, text, tables, chart values, speaker notes)
    become the content of a new file, and
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
- **A deck's slides, sources and pictures.** `write_presentation` builds a deck
  in the user's slide master, or without one in the plain master, from the
  slides the model wrote
  (`workbench/api/tools_presentations.py`). A slide's text, tables and chart
  values are file content, as are its speaker notes. Its sources go into the
  notes, after the model's own notes, under a line `Sources:`, one line a
  source as `<kind>: <reference>` (`file: ` and the file's path in the chat
  with its page, sheet or cell, `job: ` and a job's id, `user: ` and the user's
  note), or as the kind alone (`user` without a note, `assistant`). The call can
  leave them out (`include_sources: false`). A picture of the chat that a slide
  shows is copied into the deck, which then holds its bytes. The master's
  theme, layouts and media are carried into the new file;
  its example slides, its thumbnail, `docProps/app.xml` and the parts that name
  who edited it or commented on it (`ppt/authors.xml`, `ppt/commentAuthors.xml`,
  `ppt/changesInfos/`, `ppt/revisionInfo.xml`) are not. Of its core properties
  (`docProps/core.xml`) only the language is kept: the deck has no title,
  subject, keywords, comments, category, author, last editor, identifier,
  status or version, it is revision 1, and it was made and modified when it was
  built. The master, the pictures and the files a source names are only read.
  The plain master is python-pptx's own, read from the executor image: no file
  of a chat is in it (`workbench/executor/presentations/master.py:plain_master`).
- **Renders of pictures, PDFs and Office files** (`workbench/api/renders.py`,
  `workbench/executor/runners/render.py`, `workbench/executor/runners/convert.py`).
  A file the model or Ghostwriter asks to see (`POST /files/prepare`,
  `GET /files/view?rendition=`, `view_file`, `read_document`) is drawn in the
  executor into JPEG or PNG renders: a picture scaled to at most 2048 px, a PDF
  page at 1600 px, each under 768 KiB. A Word, PowerPoint or OpenDocument text
  file (`.docx`, `.dotx`, `.pptx`, `.potx`, `.odt`) is first converted once, in
  the executor, to a PDF of at most 32 MiB (`converted.pdf` in the job's `out/`),
  and its pages or slides are drawn from that PDF as a PDF's are. They are
  made from the file's own content, which can be anything a picture or a page
  shows, text and faces included. The converted PDF holds the whole of the
  file's content, text and pictures, as the source does (a conversion that may
  have left a picture out is refused, never kept); it is read by the
  render jobs alone, and no route and no tool sends it anywhere. **Every render is stripped of image metadata**:
  the source's EXIF (the GPS block among it), XMP and comments are not carried
  into it, and its colour profile is applied to the pixels, which come out in
  sRGB, and then dropped (a grey picture keeps its grey values, and a profile
  that cannot be read is dropped unapplied). A render, like the converted PDF,
  is a derived file of its source, not a file of the chat: see
  [Where it is stored](#where-it-is-stored).
- **docint's data** (`workbench/api/tools_inline.py`). The model's code and up to
  8 texts docint builds from the turn (passages of its documents, keyword
  checks, topic listings), which can hold special categories of data, with the
  user and session pseudonyms, the purpose and the request id in the headers.
  The data ends with the job (see
  [What a docint call leaves](#what-a-docint-call-leaves)).
- **Identity headers.** The caller key, `X-User-Id` (docint's or Ghostwriter's
  pseudonym) and `X-Session-Id` (docint's or Ghostwriter's session pseudonym).
  See [permissions.md](permissions.md#the-boundaries).

## Where it is stored

Everything lives on the external volume `workbench-data`, mounted at `/data`.
`workbench.executor.prepare_volume` lays it out before the services start
(names in `workbench/layout.py`):

| Path | Owner, mode | Holds |
|---|---|---|
| `/data/` | root, group of the api, `1771` | the parts below; the api may add the database's journal files here |
| `workspaces/<caller>/<user-id>/<chat-id>/…` | api, `0700` | the users' files: uploads under the names users chose, tool outputs under the names the model chose, as plain bytes |
| `workspaces/<caller>/<user-id>/<chat-id>/.derived/<source-id>/<id>.jpg` (or `.png`, or `.pdf`) | api, `0700` | the renders of a picture, a PDF or an Office file, and the PDF an Office file was converted to, each named by an id and nothing of the source's name; the folder is reserved, no path of any caller reaches it and no listing shows it |
| `staging/<job-id>/<name>` | api, `0700` | outputs the executor hands over; each is moved into its chat folder (a render, into its source's `.derived/` folder) within the same request (an inline job's are read back and removed) |
| `jobs/<job-id>/` | `jobs/` root's, `0711`; each job's folder root's, `0710`, its group the slot's; its `home/` and `out/` the slot's, `0700` | one private folder per running job: copies of its input files, its `out/`, its `home/` with its `tmp/` and its answer (`result.json`), and the request (`spec.json`); removed when the job ends |
| `workbench.db` | api, `0600` | the SQLite database, in WAL mode with `secure_delete` on |

The database has three tables (`workbench/api/store.py`):

- `files`: per file its id, caller, user id, chat id, name (its path below the
  chat folder), size, media type, kind (`upload` or `output`), creation time
  and, for an output, the job that made it;
- `derived`: per render its id, its source's id, caller, user id, chat id, role
  (`image` or `page`; `pdf` for the PDF an Office file was converted to),
  place (page number), name (`<id>.jpg`, `<id>.png` or `<id>.pdf`), size, media
  type, width and height (none for the PDF), a few facts (the strip, and on a
  file's first render what the executor found: kind, unit, page count, the pages
  without a text layer; on the PDF its pages and, for a deck, its slides) and the
  creation time **of its source**. It is bound to its source: counted in the
  user's quota, deleted with it, and reachable by no path;
- `jobs`: per tool run its id, caller, user id, chat id, tool, start time and
  status. No argument, code or result.

**File names and user ids are personal data in themselves.** A file name can
name a person or a case, and the user id identifies a user to its caller. Both are
in the database and in the folder names on the volume.

Three more places hold data for a short time:

- the api's `/tmp`, a tmpfs in memory, where uploads are spooled while they
  arrive (at most the upload limit plus 1 MiB each);
- the database's `-wal` file, which can hold earlier versions of deleted rows
  until a checkpoint copies over them (see
  [retention.md](retention.md#after-a-sweep));
- the api's memory, which remembers the refusal of a file the executor could
  not convert or draw. A refusal the file decides is remembered at once: one
  that is damaged or not what its name says, password-protected, a deck with no
  slides, a macro-enabled deck or a slide show, one with a picture over 120
  megapixels or more pictures than can be drawn within the memory limit (also
  when a conversion redone in parts runs out of its time), or one whose PDF
  would pass 32 MB. Any other `unreadable_file` (a job that could not write
  inside its sandbox, a conversion that ended for no reason the file gives) is
  remembered only when it comes twice in a row for the file, a prepare that
  went well between them starting the count again; a timeout or a busy
  executor never is. It keeps the file's id, its creation time and size, the
  refusal's fixed text, which names no file, and how many came in a row
  (`workbench/api/store.py:Store.remember_refusal`). Every later prepare, view
  or read of the file's pages is answered with a remembered refusal at once,
  with no job, and recorded as the refusal was
  ([audit.md](audit.md#what-is-recorded), `remembered`). It is never written to
  the volume. Uploading the file again makes a new file, which is tried afresh.

workbench encrypts nothing at rest. Encryption of the host's disk is the
operator's.

## How long

| What | Kept for | Enforced by |
|---|---|---|
| Uploads and outputs | `WORKBENCH_RETENTION_DAYS` (7) days from creation; using a file does not extend it, and there is no extension of any kind | `workbench/api/retention.py:Sweeper` |
| Renders, and an Office file's converted PDF | as long as their source: they carry its creation time, so they are due with it, and are recorded and deleted in the same batch. A render or PDF with no source, or one left when its source went, is swept on its own | `Sweeper._delete`, `Sweeper._sweep_derived` |
| Job records | the same period, from the job's start | `Sweeper._forget_jobs` |
| A refusal remembered or counted, in the api's memory | until its file goes (retention, or its chat's delete), the api restarts, or 4096 refusals asked for since push it out (`REFUSALS_MAX`); a new upload under the same name is a new file and inherits nothing. A job of a chat deleted while it ran leaves none. One of a file that retention removes while its job runs can stay until it is pushed out, never answered: no file of that id comes back | `Store.remove`, `Store.delete_chat`, `Store.remember_refusal` |
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

- **Tool results go to the caller that asked**: Ghostwriter or docint
  (workbench keeps neither docint's data nor its outputs). The caller puts them
  into the chat and sends them, in the model's context, to whatever model the
  operator configured behind the federation's inference router. If that model is
  run by an outside provider (a hosted API, or an Ollama `*-cloud` model), the
  results leave the host and the federation to that provider. They stay in the
  caller's chat history for its own retention (see the box at the top).
- **Image bytes go to the caller too**, as renders: in the MCP image blocks of
  `view_file` and `read_document` (for the assistant alone, at most 8 and 4
  images and 2.5 MiB an answer), and as the bytes of a rendition view
  (`GET /files/view?rendition=`) for the user's browser. Each is recorded by the
  ids of the source and the render before it leaves ([audit.md](audit.md)).
  Besides the image, the caller gets the manifest's metadata: the kind, the page
  count, the pages without a text layer, and each render's size, type, width and
  height. A render has none of the source's own image metadata.
- **Files go to Ghostwriter**, and from it to the user's browser, when the user
  views or downloads them (`GET /files/view`). What a user downloads is outside
  workbench from then on.
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

workbench itself sends nothing outside the host: the executor has no network,
and the api calls no model and no other service. What leaves the federation
leaves through the callers' model calls: docint's and Ghostwriter's.

## Facts for the record of processing and the impact assessment

These are facts about the software. The legal assessment stays with the
operator (deploy ADR 0003, "Consequences").

**Categories of data.** Whatever users upload (Ghostwriter's, by chat), or docint
sends with a call (passages of its documents), which workbench cannot know in
advance: documents, spreadsheets, e-mails, presentations, and what is in them.
That can include special categories of personal data. Pictures and scans are
among them (a photograph can show a person), and a render holds all of what its
source shows. Also what the model writes into new files (table values,
Markdown, template values, slide content) and their names. Besides: file names, chat ids, the user and session pseudonyms
of docint and Ghostwriter with the purpose and request id they send, and the
time and tool of each run.

**Categories of data subjects.** The users of docint and Ghostwriter (as
pseudonyms), and every person named in the files or data they send or in
the values the model writes.

**Where data flows.** These are the flows, as facts:

- to the caller that asked, for the same user: results to docint (workbench
  keeps neither its data nor its outputs); results, viewed files and the images
  of pictures and pages to Ghostwriter, with `outputs` and a preview of each
  csv or xlsx file, which Ghostwriter keeps for `WORKBENCH_RETENTION_DAYS`;
- through docint and Ghostwriter, to their models and their own databases: what
  a run returns enters their model context and is kept with the turn; their own
  processing, recorded there;
- to obs-plane: the audit events (user pseudonym, chat id, file ids, times,
  counts, purpose and request id) into its audit store, and the containers' log
  lines into its log store. Metrics hold no personal data.

workbench itself sends nothing outside the host. Tool results go to whatever
model the operator configured behind the inference router; if that is an
outside provider, they leave the host and the federation. Whether any of these
is a recipient in the legal sense is for the operator to decide.

**Storage periods.** Files, their renders and job records:
`WORKBENCH_RETENTION_DAYS`, 7 days by default, from creation (a render counts
from its source's). Ghostwriter's chat delete (`DELETE /files/chat`) removes
the chat's files and their renders at once and leaves an event by file id
(the renders' among them), nothing else. Leftovers of interrupted work: one hour. Audit events: obs-plane's audit
retention (`AUDIT_RETENTION`, one year by default). Copies in Ghostwriter's
chat database and docint's session database, kept with the chat turn: their own
retention; workbench keeps neither the data nor the outputs of a docint call.
Docker's `local` log driver (`docker/compose.yaml`, `x-logging`) also keeps a
copy of each container's output on the host, the api's audit lines (user
pseudonyms, chat ids, file ids, purposes and request ids) among them: rotated
by size, at most five files of 50 MB per container, not by time, so with
little traffic it can outlive obs-plane's retention.

**Technical and organisational measures.**

| Measure | Where |
|---|---|
| One workspace per user; another user's file is not found | [permissions.md](permissions.md#the-boundaries) |
| A bearer key per caller; only callers and the scraper join `workbench-net` | [configuration.md](configuration.md#the-caller-key) |
| Every file opened in a sandbox without network, under a slot uid of its own | [sandbox.md](sandbox.md) |
| Hardened containers: read-only roots, no capabilities but those listed, no privilege escalation | [sandbox.md](sandbox.md#capabilities) |
| Every file read, change, disclosure and deletion recorded by file id, fail closed | [audit.md](audit.md) |
| Files deleted after the period, each deletion recorded first | [retention.md](retention.md) |
| Every picture and page is decoded only in the sandbox, and every render is stripped of image metadata | [sandbox.md](sandbox.md#residual-risks) |
| No content, name or user id in workbench's own log lines or in metrics | [What leaves](#what-leaves) |
| No tool deletes or overwrites a file | [permissions.md](permissions.md#the-agent-deletes-nothing) |

**Decisions that stay with the operator.**

- the legal basis of the processing;
- which inference provider serves the chat model: one inside the federation,
  or an outside provider that receives the tool results;
- whether any of the flows above goes to a recipient in the legal sense;
- the outcome of the impact assessment;
- the notice to users: what workbench keeps and for how long, and that
  Ghostwriter's chat history and docint's analyses keep tool results for longer;
- whether the retention periods (workbench's, Ghostwriter's, docint's, the audit
  store's) fit the rules that apply.
