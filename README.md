# workbench

The nos-tromo federation's shared tool tier. Ghostwriter users drop files into a
chat; the model reads, searches and analyses them, looks at pictures, the
pages of PDFs and the pages and slides of Word, PowerPoint and OpenDocument text
files (each converted once in the sandbox), reads the transcript of a recording
and looks at the frames of a video (made in the sandbox from the two artifacts
Nextext keeps of a clip; workbench decodes no media), fills report templates, builds
presentations in a slide master (or a plain one) and writes new tables and
documents, all through workbench's ten typed tools. Every file is opened in a sandbox without network. Uploads, views, tool runs on
files, new files, the artifacts kept of a clip, uploads taken back and chat deletions are recorded in the federation's audit
trail, by file id; listing files (`list_files` too) is not. Every file is
deleted 7 days after it was made.

Ghostwriter's backend calls the tools through `/mcp`, with a bearer key, and
keeps a chat's files on eight file routes (list, upload, take back an upload,
keep Nextext's artifacts of a clip, prepare, measure, view, delete the chat).
docint calls `run_python` through `/mcp`, on data each call brings.

## Architecture

| Service | What it is |
|---|---|
| `api` | FastAPI, uid 10001, on the external `workbench-net` as `workbench-api:8000`, never published. The caller's door: the key, a workspace per user and a folder per chat, the tools, the audit chain, the retention sweep, the job queue and `/metrics`. It parses no user file itself. |
| `executor` | No network. A supervisor that runs every job touching file content (reading, rendering pictures and pages, converting Office files, search, Python, templates, writing) as a fresh process under one of a pool of slot uids, with limits ([docs/sandbox.md](docs/sandbox.md)). The api reaches it over a Unix socket. |
| `volume-permissions` | A one-shot that lays out the data volume before the other two start. |

State lives on the external volume `workbench-data`, so `docker compose down -v`
is safe; only `make nuke` removes it, after a typed confirmation.

Bring-up order in the federation: inference, data, obs, **workbench**, apps,
edge. workbench needs no other member to start. Ghostwriter starts and chats
work without it; only its workbench tools need it.

## Quick start

```bash
cp .env.example .env          # set DOCINT_WORKBENCH_KEY or GHOSTWRITER_WORKBENCH_KEY: openssl rand -hex 32
make network volumes          # the external workbench-net and workbench-data
make build && make up         # production shape: no host port
make health                   # both services, then the api's /healthz
```

`make up-dev` (or `make dev`, which builds first) also publishes the api on port
8100 of the host's loopback, `127.0.0.1`, for local work
([docs/configuration.md](docs/configuration.md#the-composeoverrideyaml-gotcha)).
`make help` lists every target.

## Development

```bash
uv sync --group dev --extra executor
make verify          # ruff + pyrefly, the pre-push gate
make test            # pytest
make test-isolation  # the sandbox on a real stack of its own
```

The test stacks run as their own compose projects, with their own volumes and
networks, and are always torn down; they never touch `workbench-data` or
`workbench-net`.

## Documentation

[`docs/README.md`](docs/README.md) indexes the reference:
[configuration](docs/configuration.md), [permissions](docs/permissions.md),
[data flow](docs/data-flow.md), [audit](docs/audit.md),
[retention](docs/retention.md) and [sandbox](docs/sandbox.md). `CLAUDE.md` is
the design record for contributors and coding agents.

Licence: Apache-2.0 (third-party notes in [`docs/README.md`](docs/README.md#third-party-licences)).
