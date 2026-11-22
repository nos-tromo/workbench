# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## What this repo is

`workbench` is the nos-tromo federation's shared tool tier: the place where
tools run on users' files, so that no app runs a code sandbox or office tooling
of its own ("tool execution is shared, never embedded"; Office files are
converted to pages in the executor too). Ghostwriter connects
through the MCP door (`/mcp`). Users drop files into a chat; the model lists,
reads and searches them, looks at pictures, the pages of PDFs and the pages and
slides of Word, PowerPoint and OpenDocument text files as images, runs
Python over them, inspects and fills report templates, builds presentations in a
slide master (or a plain one), and writes tables and documents, through ten typed
tools. Every file is recorded in the audit trail by id and deleted
`WORKBENCH_RETENTION_DAYS` (7) days after it was made, the renders made of it
with it.
docint, the second caller, comes in through the same door and runs Python over
data that comes with the call.

workbench holds compute tools only. Data tools stay with the data's owner:
docint's search stays in docint, and workbench never joins `data-net`.

The design is `docs/superpowers/specs/2026-11-13-workbench-design.md`, the
build plan `docs/superpowers/plans/2026-11-13-workbench-v1.md`; version 2's are
`docs/superpowers/specs/2026-11-15-v2-mcp-docint-design.md` and
`docs/superpowers/plans/2026-11-15-workbench-v2.md`; v0.3.0's are deploy's spec
`docs/superpowers/specs/2026-11-16-ghostwriter-design.md` and its plan
`docs/superpowers/plans/2026-11-16-ghostwriter-workbench-v0.3.md`; v0.5.0's is
deploy's spec `docs/superpowers/specs/2026-11-17-open-webui-retirement-design.md`;
v0.6.0's are `docs/superpowers/specs/2026-11-18-write-presentation-design.md` and
`docs/superpowers/plans/2026-11-18-write-presentation.md`; v0.7.0's are deploy's
spec `docs/superpowers/specs/2026-11-20-ghostwriter-vision-design.md` and its plan
`docs/superpowers/plans/2026-11-20-ghostwriter-vision-workbench-v0.7.md`; v0.8.0's
(Office files drawn as pages and slides) is the same spec and deploy's plan
`docs/superpowers/plans/2026-11-21-ghostwriter-vision-workbench-v0.8.md`.
Where they and `docs/` differ, `docs/` describes the code.

## Planning

For any non-trivial change (>1 file or any root-cause fix), present a plan and wait for approval BEFORE editing code. Do not start with Edit/Write on a fresh investigation.

## Architecture

```
ghostwriter-backend, docint-backend ── agent loop (native tool calls → vllm-router)
                                   │
                                   │ MCP at /mcp, Ghostwriter's file routes: Bearer key, X-User-Id, X-Session-Id
                                   │ over workbench-net (only callers and obs-plane's scraper)
                                   ▼
                           workbench / api ─── workbench-data (workspaces, staging, jobs, database)
                                   │           audit chain → stdout → obs-plane
                                   │ one job per connection, Unix socket in workbench-run
                                   ▼
                           workbench / executor (network_mode: none; one uid per slot)
```

- **`api`** (`workbench/api/`): FastAPI, one process, uid 10001. The key check,
  workspaces and path rules, the tools, the audit chain, the retention sweep,
  the job queue and `/metrics`. It never parses a user file.
- **`executor`** (`workbench/executor/`): a supervisor, root with six
  capabilities, that runs every job touching file content as a fresh process
  under a slot uid (a pool of 16, `20001`…`20016`; `WORKBENCH_EXECUTOR_SLOTS`,
  default 4, run at once), copy-in and copy-out, with limits. The
  readers, the search, the templates, the writers, `run_python`, the renders
  of pictures and PDF pages and the conversion of Office files to a PDF all run
  there.
- **`volume-permissions`**: a one-shot that lays out `workbench-data`
  (`workbench/executor/prepare_volume.py`) before the other two start.
- **Networks**: the external `workbench-net`, the federation's fourth seam.
  The api joins it as `workbench-api`, port 8000, never published; its other
  members are the callers and obs-plane's scraper. Nothing joins
  `inference-net` or `data-net`; the api calls no model.
- **State**: the external volume `workbench-data` at `/data`
  (`workbench/layout.py`): `workspaces/<caller>/<user-id>/<chat-id>/`,
  `staging/<job-id>/`, `jobs/<job-id>/` and `workbench.db` (SQLite, WAL). Only
  `make nuke` removes it. `workbench-run` holds only the executor's socket.
  The renders of a file (a picture, the pages of a PDF, the pages or slides of
  an Office file) and the PDF an Office file was converted to are its **derived
  files**: `<chat-id>/.derived/<source-id>/<id>.jpg` (or `.png`; `.pdf`, role
  `pdf`, for the conversion), each a row of the `derived` table bound to its
  source. They are as old as it, count in the user's quota, go with it, and no
  path reaches `.derived` (`layout.DERIVED`, a reserved name that no upload,
  output or folder takes). Only renders ever leave: the converted PDF is read by
  the render jobs alone.
- **Bring-up order**: inference → data → obs → workbench → apps → edge.

Shared modules: `workbench/config.py` (settings for both roles),
`workbench/paths.py` (virtual paths, ids, file names), `workbench/protocol.py`
(the job request and answer, one JSON line each), `workbench/layout.py` (names
on the volume), `workbench/tables.py` (table rules both sides apply),
`workbench/decks.py` (the bounds of a deck, which the api checks before a job and
the runner checks again, as with `tables.py`), `workbench/renders.py` (the bounds
of a render, `RenderAnswer` and its checks, `kind_of` (which names an Office file,
`OFFICE_SUFFIXES`, and `unit_of` whether its pages count as pages or slides), the
converted PDF's name, type and cap, the strip rule and `render_name`, which the
api checks the executor's answer against; it imports no imaging library and
nothing of LibreOffice). The executor imports nothing of the api, and the api never
decodes a file: it imports neither `PIL`, `pypdfium2` nor `pi_heif`, and checks
a render's signature and size alone. A file's facts (`RenderFacts`) are kept in
the row of its page 1 render and read back leniently, an unknown one ignored, so
**a field added to `RenderFacts` needs a default**: each version then reads the
heads the other prepared, and a rollback never leaves a file that always 500s.

## The tool core and the doors

**The tools are rows.** Each workspace tool is a row of
`workbench/api/tool_table.py:WORKSPACE_TABLE`: its name, a description written
for the model, the models of its arguments and its answer, and the function that
runs it. The door tells each caller the tools it may call (`tools/list`, frozen
in `tests/api/golden/`). The ten: `list_files`, `read_document`,
`search_files`, `view_file`, `run_python`, `inspect_template`, `fill_template`,
`write_table`, `write_document`, `write_presentation`.

**Images only for a call that takes them.** A call on `/mcp` that does not send
`X-Accept-Images: 1` (`workbench/api/auth.py:accepts_images`; Ghostwriter 0.3.0
sends it) is told and shown what 0.6 was: no `view_file` (its row is
`images_only`), `read_document` told its row's `text_description` (0.6's words)
and read as text alone, `write_presentation` told its row's `text_description`
(0.8.0's words, which name no `view_file`), the read job's hint in 0.6's words
(`images` in its arguments), the 0.6 server instructions
(`mcp_wire.WORKSPACE_TEXT_INSTRUCTIONS`), no image block. The schemas are the
same both ways. The goldens hold the listing with the header;
`tests/api/test_tools_golden.py` derives the one without. The header selects
what the model is told and shown, never what a caller may reach.

- `workbench/api/tools.py`: `list_files`, `search_files`, `run_python`, the
  models of `read_document`, and the helpers the other tool modules share.
- `workbench/api/tools_views.py`: `read_document` and `view_file`, the tools that
  show pictures, pages and slides as images (an Office file's too; a deck's are
  `slide N`, `ViewedFile.unit` says which): MCP image blocks for the assistant
  alone,
  after the answer's text, at most 8 (`read_document`: 4) and 2.5 MiB raw, image
  block `i` being `shown[i]`, always. `workbench/api/renders.py` prepares and
  reads the renders back and imports `tools.py`, so `tools.py` never imports it,
  and `read_document`'s function lives here. It decides which render jobs run
  (`prepared`, `renders_for`: a file or a page whose renders exist is answered
  from the rows without the source's lock, and no job runs when the source's
  renders leave no room for one more; an Office file with no `pdf` row is
  converted first, under the source's lock, in the same turn as its drawing, a
  turn of the caller's conversion gate, and one whose PDF has gone is converted
  again so too; a file the executor refused for what it is is refused again at
  once, with no job, from `Store.refusal_of`) and reads each render back by
  its row,
  never one that is not what the row says. A render becomes visible only after its `modify` /
  `file.prepare` event, and leaves only after its `disclose` / `file.view`,
  `query` / `tool.view_file` or `query` / `tool.read_document` event names it.
- `workbench/api/tools_templates.py`, `workbench/api/tools_output.py`: the
  template and output tools. A new tool gets a module of its own, as
  `workbench/api/tools_presentations.py` (`write_presentation`) has: it first
  reads a block in a shape models write by mistake as the block meant
  (`_read_as_meant`: a place's `role` dropped, a block or list written as JSON in
  a string, `{image: …}`, line breaks written out before a list's next item),
  then checks the master, the name, every deck bound, each picture and each
  source before a job, so a refusal names its slide and block and costs no job.
  A slide that names no layout is left for the runner, which takes the first
  layout that takes it (`presentations/deck.py:fitting`), and a title slide's
  only text block is its subtitle (`subtitle_as_meant`).
- `workbench/api/files.py`: Ghostwriter's file routes (list, upload, taking back
  an upload not yet sent, prepare, view, the chat delete). `POST /files/prepare` renders a picture, or the first 20
  pages of a PDF or an Office file, once (idempotent, answered from the rows after
  that; an Office file is converted first and then drawn, two jobs in one turn of
  the conversion gate; with `strips` 2 or 3 it also cuts a picture into strips, on
  the prepare gate) and `GET /files/view?rendition=` serves one render, made first
  on the user's own turn when it does not exist yet.
- `workbench/executor/runners/`: one runner per tool that needs file content
  (`RUNNERS`); `presentations.py` is `write_presentation`'s, `render.py` is
  `prepare_file`'s, which the render routes and tools run, and `convert.py` is
  `convert_office`'s: it runs the checks LibreOffice must not be the first to meet
  (the zip guard, an OpenDocument manifest that says it is encrypted; a deck
  with no slides, a macro-enabled one or a slide show is `unsupported_format`;
  the size of every picture, from its header), has `render/office.py` convert
  the file to one PDF in a folder of the job's tmp (whole, or in page ranges
  joined by pdfium when the whole came to the address-space ceiling: see
  Gotchas), checks that PDF (a regular file, at most 32 MiB, the `%PDF-`
  signature, pdfium counts its pages) and only then moves it to
  `out/converted.pdf`. `prepare_file` then draws the pages from that PDF, which
  the api hands it at its derived path, never one a caller names.
  `workbench/api/jobs.py:JobQueue` sends the job, keeps what it made and settles
  its record. A render job (`submit_renders`) keeps its outputs as derived files
  of its source (`Store.register_derived`), never as files of the chat. **The
  prepare gate and the conversion gate:** a prepare, the render job of a picture or
  a PDF just attached (`gate="prepare"`), waits for its caller's prepare gate in
  place of its user's turn, so it never takes the user's interactive one; an Office
  file's conversion and its drawing, one turn (`gate="conversion"`), wait for its
  caller's conversion gate, whether a prepare or a call on demand runs them. Each
  gate runs one job of a caller at a time, over all its users, within the caller's
  slots, and at most `PREPARE_WAITING_MAX` wait at each, counted apart from the
  queue and from each other: a picture is prepared while a deck converts, and a
  caller never converts two files at once. Other renders on the routes and in the
  tools run on the user's own turn.
- `workbench/executor/render/`: what `prepare_file` uses. `images.py` decodes a
  picture (its content decides the format, not its name; at most 120 MP; one HEIF
  decoder thread), brings it to the bounds of a render, to sRGB through its
  colour profile, and upright;
  `encode.py` writes JPEG or PNG under 768 KiB with none of the source's
  metadata, and cuts strips; `pages.py` opens a PDF with pdfium, finds the pages
  with no text and draws one at 1600 px. Imported by the executor only.
  `office.py` is `convert_office`'s: LibreOfficeKit loaded through
  ctypes in a **forked child** (it starts once per process and a hung C call can
  only be stopped by killing the process), with a profile of its own, filter data
  that draws hidden slides, leaves out notes pages and keeps pictures to 150 dpi,
  and the image's bake (`python -m workbench.executor.render.office bake <dir>`).
  It imports only the standard library and the readers' errors; LibreOffice is
  loaded in that child alone, never in the job's process, and `soffice` is never
  run.
- `workbench/executor/presentations/`: what the runners of a slide master use.
  `master.py` reads the layouts and their places (`inspect_template`) and opens a
  `.pptx` or `.potx`, or makes the plain master (`plain_master`: python-pptx's
  own, nine layouts, widened to 16:9) when the call names none; `deck.py` removes
  the master's example slides and builds the slides; `blocks.py` fills a place
  with a table, a chart or a picture; `text.py` writes Markdown into a text
  frame. A deck keeps the master's theme, layouts and media, of its core
  properties only the language, and none of its example slides, its thumbnail
  or its `docProps/app.xml`; a master that links to its own slides is refused
  (`invalid_template`).
  `workbench/executor/readers/ooxml.py` gives a template (`.dotx`, `.potx`) the
  content type of a document or a presentation so that a library opens it, and
  tells a macro-enabled presentation or a slide show, which are refused.
- `workbench/api/registry.py`: every tool as a `ToolSpec` of its form
  (`workspace` or `inline`), and what a caller may call. `tools_inline.py` holds
  the inline tools (`run_python` on data that comes with the call, outputs
  returned and never kept). `mcp.py` is the MCP door's dispatch and
  `mcp_wire.py` its wire rules, with no I/O.
- `workbench/api/tools_mcp.py`: Ghostwriter's ten workspace tools on `/mcp`,
  each calling its row's function. A call's `structuredContent` is the
  function's answer plus `outputs`, each file it wrote with its id and, for csv
  and xlsx, a preview that `workbench/executor/preview.py` reads in the job. The
  text block carries no previews and is not re-cut after `outputs`; 5 s of the
  wall (`PREVIEW_RESERVE_S`) are kept for the previews.

Adding a tool touches: its row in `tool_table.WORKSPACE_TABLE`, or for an
inline tool its spec in `tools_inline.INLINE_SPECS` and a name in
`registry.INLINE_TOOLS`; its runner and `jobs.TOOLS`, if it runs in the
executor; `audit.OPERATIONS`, if it is recorded;
`config.GHOSTWRITER_DEFAULT_TOOLS`, the `GHOSTWRITER_WORKBENCH_TOOLS` default in
`docker/compose.yaml` and the tool list of `.env.example` (the three agree;
`tests/test_config.py` and `tests/test_compose_defaults.py` read them);
`docs/permissions.md` and `docs/audit.md` (`tests/test_docs.py` fails without
them); and the golden `tools/list` files, edited on purpose, never regenerated.
The start check refuses a caller's tool list that names a tool of another file
form.

Every router is built with `route_class=EncodableRoute`
(`workbench/api/routing.py`: a JSON body read within a depth limit, lone
surrogates replaced); a new one must be too. A request meets the middleware
in this order (`workbench/api/main.py`): `ViewHardening`, `QuietErrors`,
`KeyGate`, `BodyLimit`, then the route.

Every route that is not registered answers 404 (401 without the key, which is
checked before any body is read). A trailing slash is a 404 too, never a 307
(`redirect_slashes=False`).

**Callers.** `workbench/api/auth.py:CallerRegistry` holds the callers by name,
key, file form, allowed tools and caps. Two, both coming in through `/mcp`, each
with a file form: `docint` (inline data, `DOCINT_WORKBENCH_TOOLS`, with its own
wall cap and running-job cap) and `ghostwriter` (`/mcp` and six file routes,
workspace files, the ten tools, its own caps, confined to its chat). The key
picks the caller; the door answers 404 on any other caller's paths. The api
starts with either `DOCINT_WORKBENCH_KEY` or `GHOSTWRITER_WORKBENCH_KEY`.

## The boundaries

`docs/permissions.md` has them in full. In short:

- **The user is the boundary; the chat is a folder.** The caller is its bearer
  key; the user is `X-User-Id`, a keyed pseudonym of docint's or Ghostwriter's,
  resolved through the caller's own audit event for the call (same request id);
  the chat is `X-Session-Id`, or `default`. Every path resolves
  below the user's root (`workbench/paths.py:Workspaces`); another user's file
  is a 404.
- **Files live only inside chat folders.** Nothing is ever a file directly
  under `/workspace`. Renders live in the chat's reserved `.derived/` folder,
  which no path of any principal reaches.
- **Every render is made in the executor and stripped.** A picture or PDF is
  decoded only in a job (Pillow, pi-heif, pdfium: hostile input, `docs/sandbox.md`
  § Residual risks), and what comes back is a JPEG or PNG with none of the
  source's metadata (no EXIF, GPS, ICC or XMP). The api trusts a render's stated
  width and height only within the bounds, so a wrong value costs tokens, never
  access.
- **Every conversion runs in the executor too.** A Word, PowerPoint or
  OpenDocument text file reaches LibreOffice only there, after the zip guard and
  the encryption checks, in a forked child under the job's limits, with macros
  off and no network (`docs/sandbox.md` § Residual risks). The api never converts:
  it keeps the one PDF a conversion made, checking its signature and size alone.
- **No network** in the executor, and no socket in a job (a seccomp filter of
  workbench's own; LibreOfficeKit in-process needs none). A job sees copies of
  its own chat's files, read-only (an
  inline job, docint's, sees only the data its call brought), and writes only
  its `out/` and its private `home/` (with its `tmp/`), both removed with the
  job, inside a job folder no other slot can pass. A watchdog holds each running
  job to its disk and memory.
- **The agent deletes nothing.** No tool deletes or overwrites a file; Ghostwriter's
  chat delete, its taking back of an upload (Ghostwriter calls it only for one
  its user has not sent; workbench deletes any upload of the chat), and
  retention do.
- **Never a name in a log or an event.** Audit events name files by id; error
  answers are short fixed texts (`workbench/api/errors.py`); no handler logs a
  traceback or a cause of a path or store error. An error no handler expected is
  a 500 `internal error`, logged by method, route template and type alone
  (`errors.QuietErrors`); the supervisor logs such errors by type too. An audit
  event names at most 200 file ids; more go on `<operation>.ids` continuations.
- **Fail closed.** An action whose audit event cannot be written does not
  happen; a retention record that cannot be written stops the api.

## Container hardening (deploy ADR 0001)

Every service has a read-only root, `no-new-privileges`, `cap_drop: [ALL]` and
a `/tmp` tmpfs; the api and the executor also `ulimits: core: 0`. The api runs
as uid 10001 with no capability. Two root services add back only what they
need: the executor `SETUID`, `SETGID`, `CHOWN`, `FOWNER`, `DAC_OVERRIDE`,
`KILL`; `volume-permissions` `CHOWN`, `FOWNER`, `DAC_READ_SEARCH`.
`docs/sandbox.md` justifies each one; the executor refuses to start unless the
container is set up as compose sets it.

## Audit and retention

- **Audit** (`workbench/api/audit.py`, `docs/audit.md`): one chain per api
  process, `service: workbench`, emitter `workbench` / `api` in obs-plane's
  verifier, `AUDIT_OUTPUT=container`. `workbench/utils/audit_event.py` and
  `workbench/utils/audit_runtime.py` are vendored verbatim from
  `nos-tromo/.github` (`configs/audit/`); never edit them, CI fails on drift.
  A purpose on the MCP door and Ghostwriter's file routes (`X-Audit-Purpose`,
  `AUDIT_REQUIRE_PURPOSE`); no `recipient` on `disclose`.
- **Retention** (`workbench/api/retention.py`, `docs/retention.md`): every file
  is deleted `WORKBENCH_RETENTION_DAYS` after it was made, each batch recorded
  first, the renders of a file with it (a batch names its files, then their
  renders); leftovers of interrupted work after an hour, recorded first, and
  renders with no source (`rule: derived`); empty folders and old job records
  without a record. A changed period applies at
  once, with no notice. `make retention-dry-run [DAYS=n]`.

## Commands

Python 3.12 (`requires-python = ">=3.12,<3.13"`), `uv`, run from the repo root.

```bash
uv sync --group dev --extra executor   # dev tools + the executor's libraries
make verify               # pre-commit: ruff + pyrefly (tracked files; git add new ones first)
make test                 # pytest (no frontend/ here)
uv run pytest tests/api/test_files.py -q -k <name>   # one file, or one test
make test-executor-root   # executor tests as root in its image, in both /dev/shm shapes
make test-isolation       # the sandbox on a real stack (project workbench-test)
make test-load            # the 10-user load test on that stack (manual, not in CI)

cp .env.example .env      # set one of the two caller keys (32+ characters)
make network volumes      # the external workbench-net and workbench-data
make build && make up     # production shape, no host port; make dev = build + up-dev (:8100)
make health               # container status, then the api's /healthz from inside
make retention-dry-run    # what retention would delete now: counts and ages only
make bundle               # airgap bundle of both images, from the latest release tag
make nuke                 # DESTROY workbench-data (type 'workbench' to confirm); no audit event records it
```

`make/common.mk` and `scripts/bundle-lib.sh` are vendored verbatim from
`nos-tromo/.github` like the audit modules; never edit them.

How the tests are laid out:

- `pyproject.toml`'s `addopts` deselects the `isolation` and `load` markers;
  those run only through their make targets, on a live stack.
- The api tests (`tests/api/conftest.py`) run the real `create_app` on a
  throwaway data folder, audit lines kept in a `StringIO`. The executor is a
  `FakeClient` (`tests/api/test_jobs.py`); its subclass `Local`
  (`tests/api/test_tools_templates.py`) runs the real `RUNNERS` in process, and
  `Scripted` (`tests/api/test_tools_documents.py`) gives canned answers.
- The executor tests that switch to slot uids skip unless they run as root on
  Linux, so `make test` on a Mac passes without them; only
  `make test-executor-root` (and CI) runs them.
- Test documents are built at test time (`tests/fixtures/make_fixtures.py`,
  `make_templates.py`), all text invented; no binary fixture is committed.
- `WORKBENCH_TEST_TIME_SCALE` (default 1, CI 3) widens the tests' time bounds
  on a slow host.
- `tests/test_compose_defaults.py` reads compose, the Makefile and CI as files:
  changing a value there can fail it.

## Configuration

`docs/configuration.md` lists every variable. `.env.example` follows deploy's
shared format; check it with `python3 -B ../deploy/scripts/check_env_examples.py .`.
The settings live in `workbench/config.py`; compose gives the api and the
executor one shared block (`x-settings`), since the executor bounds every job by
its own copy. `WORKBENCH_DATA_DIR`, `WORKBENCH_EXECUTOR_SOCKET` and
`WORKBENCH_SLOT_UID_BASE` are tied to the images and mounts and are never set
in compose. `DOCINT_WORKBENCH_KEY` holds the same value as docint's
`WORKBENCH_API_KEY` and `GHOSTWRITER_WORKBENCH_KEY` as Ghostwriter's; docint's
and Ghostwriter's variables and `AUDIT_REQUIRE_PURPOSE` are in the api's
`environment:` alone, since the executor reads none of them.

## Gotchas (verified)

- **The font cache is built into the executor image.** Every `run_python` job
  imports matplotlib, which rescans the fonts when its config folder is not
  writable. The image scans them once (`/opt/mpl-cache`, root's, read-only), and
  each job copies that cache into a config folder in its private tmp
  (`workbench/executor/runners/python.py`). A change of the base image or of
  matplotlib rebuilds it with the image.
- **LibreOffice's profile is baked, and each conversion copies it.** The image
  starts LibreOfficeKit once at build time and keeps the profile
  (`/opt/lo-profile`, root's, read-only: the macro and link settings of
  `office.PROFILE_SETTINGS`, warm), and each conversion copies it into a folder
  of the job's private tmp (`office.seed_profile`), as the font cache is copied,
  since LibreOffice writes to its profile. LibreOfficeKit starts once per process
  and cannot start again, so each conversion forks a child of the job
  (`office.run_in_child`), which ends in `os._exit` without `destroy`. A hung
  LibreOffice call is stopped only by killing that child: the parent polls to its
  deadline, then kills and reaps it. The image has no Java and no Python script
  provider (`--no-install-recommends`), and the executor image is about 515 MB
  larger for LibreOffice and its fonts.
- **LibreOffice drops pictures at the address-space ceiling and says it
  succeeded.** Under the job's `RLIMIT_AS` (1536 MB, never raised for it) it
  holds every distinct picture it has decoded until the PDF is saved; one that no
  longer fits is left out, the PDF saved, success reported. Nothing in the PDF
  tells, so the child reads its own `VmPeak` after the save and exits
  `office.EXIT_AT_CEILING` when it came within a headroom of the limit: the
  largest picture at 4 bytes a pixel (Pillow reads every member's header in the
  job, whatever its name) and 128 MiB. A conversion at the ceiling is never
  kept: `runners/convert.py` converts the file again in page ranges, a fresh
  child each, halving a range that comes to the ceiling again, joins the parts
  with pdfium, and refuses with `office.TOO_MANY_PICTURES` when a single page
  still does or the budget runs out. `docs/sandbox.md` § Limits has the numbers.
- **A call waits longer for a source's lock than for its turn.** A view or a
  tool waits 5 s for its turn (`MCP_QUEUE_WAIT_S`), but up to 120 s for the lock
  of a source whose renders another call is making (`SOURCE_LOCK_WAIT_S`):
  Ghostwriter prepares an attachment as it arrives, and a deck's conversion takes
  13-31 s, so a view of it waits for that prepare rather than answer busy, and
  converts nothing again. 120 s is one job wall, what the call would have spent
  converting itself, and short of Ghostwriter's 150 s for a tool call. The wait
  holds nothing else; every path takes the lock first and a turn second. A call
  that converts a file on demand waits for the conversion gate and then a slot
  up to the same 120 s, past its 5 s, since it waits out another file's
  conversion. The two waits are apart, so the worst case of a view that converts
  (both waits, then two walls) passes Ghostwriter's 150 s; the usual one does not.
- **A refusal is remembered, in memory alone.** A file the executor refused at its
  prepare or its conversion is answered with that refusal by every later prepare
  or render, with no job, once `Store.refusal_of` holds it (an LRU of
  `REFUSALS_MAX`, by the source's id, creation time and size), and recorded as
  the refusal was, with `remembered: true`. A refusal the file decides is
  remembered at once: every `unsupported_format`, and an `unreadable_file` whose
  text is in `renders.FILE_DECIDED` (damaged, encrypted, a picture over 120 MP,
  too many pictures, also when a conversion redone in parts runs out of its time,
  no pages, a PDF past 32 MB). `FILE_DECIDED` is an allow-list of copies of the
  runners' texts (the api imports nothing of the executor), pinned to their
  constants by `tests/api/test_renders.py`. Any other `unreadable_file` (a write
  that failed in the job's sandbox, a conversion that ended for no reason the file
  gives, a text added later) is remembered only when it comes twice in a row, a
  job that went well between them starting the count again; a timeout, a busy
  executor or a failure never is. It goes with the file (`Store.remove`,
  `Store.remove_upload`, `Store.delete_chat`; a job of a deleted chat remembers
  nothing) and with a
  restart; uploading the file again makes a new file, tried afresh.
- **The executor's healthcheck reads `/proc/net/unix`.** It looks for the
  socket's path with the listening flag `00010000` and never connects. The
  socket file alone is not enough: it outlives a killed supervisor in
  `workbench-run`. The probe is in `docker/compose.yaml`;
  `make test-executor-root` mounts `docker/` so its test runs in the image.
- **No multiprocessing in jobs.** `/dev/shm` is root's and
  `fs.mqueue.queues_max` is 0, so `multiprocessing` and process pools fail in a
  job; threads work. The supervisor refuses to start without that shape. The
  root executor tests run in both shapes; the tests that need Docker's default
  `/dev/shm` are marked `SHARED_IPC_ONLY`.
- **The test stacks are their own projects.** `make test-isolation` and
  `make test-load` use `docker/compose.test.yaml` (project `workbench-test`,
  volume `workbench-test-data`, network `workbench-test-net`, `:test` images,
  no `.env`). Each is torn down with `down -v` by a trap on every way out.
  Never point them at `workbench-data` or `workbench-net`.
- **The test process limits BLAS threads.** `tests/conftest.py` sets
  `OPENBLAS_NUM_THREADS` and `OMP_NUM_THREADS` to 1 before anything imports
  numpy, as every job has them: the timer tests hold `SIGALRM` back in one
  thread, and another thread would take the signal.
- **A bare `docker compose` from `docker/` publishes the api.** It loads
  `compose.override.yaml` on its own, which publishes `127.0.0.1:8100`.
  Use `make`, or `docker compose --env-file .env -f docker/compose.yaml` from the
  repo root.
- **`WORKBENCH_EXECUTOR_CPUS` must not pass the host's CPUs.** Docker refuses to
  start the executor otherwise (default 4); CI lowers it on small runners.
- **`.gitignore` ignores `*.txt`, `*.sh`, `*.log` and `*.ipynb` everywhere.**
  Only `scripts/bundle_images.sh` and `scripts/bundle-lib.sh` are excepted. A
  new file of those types is silently untracked, so pre-commit and CI never see
  it; check with `git check-ignore -v <path>`.

## Git & PR Workflow

- Never commit directly to `main`; always branch (`feat/`, `fix/`) and open a PR.
- Never create a NEW PR when an existing PR for the work is open — push additional commits to that branch.
- Release order is strict: bump `[project].version` in `pyproject.toml` and run `uv lock` in the release PR. On merge, `.github/workflows/release-tag.yml` mints the annotated `vX.Y.Z` tag. Never tag by hand.
- Use single, non-compound shell commands for `gh` operations (no `&&` chains); if `gh pr merge` is blocked, fall back to the GitHub MCP merge tool.

## Verification

Before claiming a check is green, run the actual command and paste the output. `pre-commit run --all-files` (and so `make verify`) hands ruff the tracked files only, as `git ls-files` lists them, while pyrefly (`pass_filenames: false`) checks the whole tree: `git add` new files first. After opening a PR, confirm CI actually triggered on the latest push before declaring done.

## Communication Style

Examples, fixtures, tests and docs use synthetic names only (`user-a`, `chat-1`,
`quarterly-figures.xlsx`, `harbour-report.pdf`), and `<EDGE_HOST>` for a host.

Keep changes minimal and scoped. Do not add explanatory code comments for trivial or self-evident changes. Do not overwrite existing test files with Write — use Edit to append or modify tests.
