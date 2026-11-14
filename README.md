# workbench

The nos-tromo federation's shared tool tier. Open WebUI users drop files into a
chat; the model reads, searches and analyses them, fills report templates and
writes new tables and documents, all through workbench's typed tools. Every
file is opened in a sandbox without network, every access is recorded in the
federation's audit trail, and every file is deleted 7 days after it was made.

Open WebUI connects to workbench as a "terminal server", with a bearer key. Its
browser uploads chat attachments straight into workbench through Open WebUI's
proxy, and the model's tools are the operations of workbench's
`/openapi.json`.

## Architecture

| Service | What it is |
|---|---|
| `api` | FastAPI, uid 10001, on the external `workbench-net` as `workbench-api:8000`, never published. The caller's door: the key, a workspace per user and a folder per chat, the tools, the audit chain, the retention sweep, the job queue and `/metrics`. It parses no user file itself. |
| `executor` | No network. A supervisor that runs every job touching file content (reading, search, Python, templates, writing) as a fresh process under one of a pool of slot uids, with limits ([docs/sandbox.md](docs/sandbox.md)). The api reaches it over a Unix socket. |
| `volume-permissions` | A one-shot that lays out the data volume before the other two start. |

State lives on the external volume `workbench-data`, so `docker compose down -v`
is safe; only `make nuke` removes it, after a typed confirmation.

Bring-up order in the federation: inference, data, obs, **workbench**, apps,
edge. workbench needs no other member to start; Open WebUI needs workbench.

## Quick start

```bash
cp .env.example .env          # set OPEN_WEBUI_WORKBENCH_KEY: openssl rand -hex 32
make network volumes          # the external workbench-net and workbench-data
make build && make up         # production shape: no host port
make health                   # both services, then the api's /healthz
```

`make up-dev` (or `make dev`, which builds first) also publishes the api on
`localhost:8100` for local work. Open WebUI's side of the connection is set up
in open-webui-service. `make help` lists every target.

## Development

```bash
uv sync --group dev --extra executor
make verify          # ruff + pyrefly, the pre-push gate
make test            # pytest
make test-isolation  # the sandbox on a real stack of its own
make test-contract   # against the Open WebUI image open-webui-service deploys
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
