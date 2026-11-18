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
of its own ("tool execution is shared, never embedded"). Ghostwriter connects
through the MCP door (`/mcp`). Users drop files into a chat; the model lists,
reads and searches them, runs Python over them, inspects and fills report
templates, and writes tables and documents, through eight typed tools. Every
file is recorded in the audit trail by id and deleted `WORKBENCH_RETENTION_DAYS`
(7) days after it was made. docint, the second caller, comes in through the
same door and runs Python over data that comes with the call.

workbench holds compute tools only. Data tools stay with the data's owner:
docint's search stays in docint, and workbench never joins `data-net`.

The design is `docs/superpowers/specs/2026-11-13-workbench-design.md`, the
build plan `docs/superpowers/plans/2026-11-13-workbench-v1.md`; version 2's are
`docs/superpowers/specs/2026-11-15-v2-mcp-docint-design.md` and
`docs/superpowers/plans/2026-11-15-workbench-v2.md`; v0.3.0's are deploy's spec
`docs/superpowers/specs/2026-11-16-ghostwriter-design.md` and its plan
`docs/superpowers/plans/2026-11-16-ghostwriter-workbench-v0.3.md`; v0.5.0's is
deploy's spec `docs/superpowers/specs/2026-11-17-open-webui-retirement-design.md`.
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
  readers, the search, the templates, the writers and `run_python` all run
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
- **Bring-up order**: inference → data → obs → workbench → apps → edge.

Shared modules: `workbench/config.py` (settings for both roles),
`workbench/paths.py` (virtual paths, ids, file names), `workbench/protocol.py`
(the job request and answer, one JSON line each), `workbench/layout.py` (names
on the volume), `workbench/tables.py` (table rules both sides apply). The
executor imports nothing of the api.

## The tool core and the doors

**The tools are rows.** Each workspace tool is a row of
`workbench/api/tool_table.py:WORKSPACE_TABLE`: its name, a description written
for the model, the models of its arguments and its answer, and the function that
runs it. The door tells each caller the tools it may call (`tools/list`, frozen
in `tests/api/golden/`). The eight: `list_files`, `read_document`,
`search_files`, `run_python`, `inspect_template`, `fill_template`,
`write_table`, `write_document`.

- `workbench/api/tools.py`: `list_files`, `read_document`, `search_files`,
  `run_python`, and the helpers the other tool modules share.
- `workbench/api/tools_templates.py`, `workbench/api/tools_output.py`: the
  template and output tools. A new tool gets a module of its own.
- `workbench/api/files.py`: Ghostwriter's file routes (list, upload, view, the
  chat delete).
- `workbench/executor/runners/`: one runner per tool that needs file content
  (`RUNNERS`). `workbench/api/jobs.py:JobQueue` sends the job, keeps what it made
  and settles its record.
- `workbench/api/registry.py`: every tool as a `ToolSpec` of its form
  (`workspace` or `inline`), and what a caller may call. `tools_inline.py` holds
  the inline tools (`run_python` on data that comes with the call, outputs
  returned and never kept). `mcp.py` is the MCP door's dispatch and
  `mcp_wire.py` its wire rules, with no I/O.
- `workbench/api/tools_mcp.py`: Ghostwriter's eight workspace tools on `/mcp`,
  each calling its row's function. A call's `structuredContent` is the
  function's answer plus `outputs`, each file it wrote with its id and, for csv
  and xlsx, a preview that `workbench/executor/preview.py` reads in the job. The
  text block carries no previews and is not re-cut after `outputs`; 5 s of the
  wall (`PREVIEW_RESERVE_S`) are kept for the previews.

Adding a tool touches: its row in `tool_table.WORKSPACE_TABLE`, or for an
inline tool its spec in `tools_inline.INLINE_SPECS` and a name in
`registry.INLINE_TOOLS`; its runner and `jobs.TOOLS`, if it runs in the
executor; `audit.OPERATIONS`, if it is recorded; `docs/permissions.md` and
`docs/audit.md` (`tests/test_docs.py` fails without them); and the golden
`tools/list` files, edited on purpose, never regenerated. The start check
refuses a caller's tool list that names a tool of another file form.

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
wall cap and running-job cap) and `ghostwriter` (`/mcp` and four file routes,
workspace files, the eight tools, its own caps, confined to its chat). The key
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
  under `/workspace`.
- **No network** in the executor, and no socket in a job (a seccomp filter of
  workbench's own). A job sees copies of its own chat's files, read-only (an
  inline job, docint's, sees only the data its call brought), and writes only
  its `out/` and its private `home/` (with its `tmp/`), both removed with the
  job, inside a job folder no other slot can pass. A watchdog holds each running
  job to its disk and memory.
- **The agent deletes nothing.** No tool deletes or overwrites a file; Ghostwriter's
  chat delete and retention do.
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
  first; leftovers of interrupted work after an hour, recorded first; empty
  folders and old job records without a record. A changed period applies at
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
