# Configuration

workbench reads its settings from the environment
(`workbench/config.py:load_settings`). A single `.env` at the repo root, copied
from `.env.example`, sets them; the Makefile passes it to compose with
`--env-file .env`. `.env.example` follows deploy's shared format (deploy
`docs/runbooks/env-sync.md`, "For member authors"); check it with:

```bash
python3 -B ../deploy/scripts/check_env_examples.py .
```

## Rules for every value

- Every number is a whole number above zero, in ASCII digits. Anything else
  stops the service at start, with a message that names the variable and never
  its value.
- An empty value, or one of only spaces, means the default.
- Both services read the same settings: compose gives the api and the executor
  one shared block (`x-settings` in `docker/compose.yaml`). The executor bounds
  every job by its own copy, so the two must agree.
- A value exported in the shell that runs `make up` beats `.env`, since compose
  interpolates `${VAR:-default}` from the shell first.

## Callers

| Variable | Default | Meaning |
|---|---|---|
| `OPEN_WEBUI_WORKBENCH_KEY` | none | The bearer key Open WebUI presents on every call. At least 32 characters once surrounding spaces are stripped. Unset, Open WebUI's door is off. Generate it with `openssl rand -hex 32`. |
| `DOCINT_WORKBENCH_KEY` | none | The bearer key docint presents on its MCP door (`/mcp`). The same rules; unset, docint's door is off. |
| `DOCINT_WORKBENCH_TOOLS` | `run_python` | The tools docint may call, comma-separated; each needs an inline form. |
| `DOCINT_WORKBENCH_WALL_S` | 60 | Wall-clock seconds one docint job may run: from 5 to `WORKBENCH_JOB_WALL_S`. A call's `time_limit_s` is a strict integer in that range (`30.0` is refused). |
| `DOCINT_WORKBENCH_SLOTS` | 2 | Jobs docint may have running at once; fewer than `WORKBENCH_EXECUTOR_SLOTS`. |

The api needs at least one of the two keys, or it refuses to start; two callers
with the same key stop the start too.

### The caller key

`OPEN_WEBUI_WORKBENCH_KEY` is shared with open-webui-service, which presents
the same value on its terminal-server connection. Both `.env` files must hold
it; change it in both and restart both stacks. It is not Open WebUI's
forwarding secret and not the inference router's key. deploy's `make env`
copies it between the two files: the key has its row (`workbench-key`) in
deploy's `env-sync.conf`, the `[shared: open-webui-service]` tag in
`.env.example` names it, and `make env ENV_GENERATE=1` generates one where it
is unset in both. From open-webui-service v0.14.0 the key also switches the
connection on: its `ENABLE_TOOL_SERVERS` follows the key, so without it there
Open WebUI runs without workbench. `DOCINT_WORKBENCH_KEY` is shared the same
way with docint, whose `WORKBENCH_API_KEY` holds the value.

The keys are compared in constant time (`workbench/api/auth.py`). They reach only
the api: the executor never reads them.

## Network

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_NET` | `workbench-net` | Name of the external network between the api and its callers; `make network` creates it. Change it only if you renamed the network. |

The api joins it as `workbench-api` on port 8000, exposed and never published.
Only callers (Open WebUI, docint) and obs-plane's scraper join it. The executor
has no network.

## Jobs

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_EXECUTOR_SLOTS` | 4 | Jobs the executor runs at once, one slot uid each; at most 16, the size of the uid pool. |
| `WORKBENCH_QUEUE_MAX` | 16 | Jobs that may wait for a free slot. Past it a call answers `503` "busy, retry later" at once. |
| `WORKBENCH_QUEUE_WAIT_S` | 60 | Seconds a job may wait for its turn; then `503`. |
| `WORKBENCH_JOBS_PER_USER` | 1 | Jobs one user may have **running** at once. Their further calls wait, and hold no slot while they wait. |
| `WORKBENCH_JOB_WALL_S` | 120 | Wall-clock seconds a job may run; also its CPU-time limit. |
| `WORKBENCH_JOB_MEMORY_MB` | 1536 | Address space one job may map, in MB. `WORKBENCH_EXECUTOR_MEM_LIMIT` must hold the slots times this. |
| `WORKBENCH_RESULT_MAX_KB` | 16 | Largest answer a tool returns to the model, in KB: 1 to 256. Longer answers are cut and say how to go on. Also the largest text file `/files/read` sends. |

How the queue decides (`workbench/api/jobs.py:JobQueue`):

- Each user also has a cap on **waiting** calls:
  `WORKBENCH_QUEUE_MAX / WORKBENCH_EXECUTOR_SLOTS`, rounded down, at least 1
  (4 by default). A user past it gets `503` at once, and the other users are not
  affected: one user cannot fill every waiting place.
- docint's calls wait at most 5 s for their turn (`MCP_QUEUE_WAIT_S`, fixed; not
  `WORKBENCH_QUEUE_WAIT_S`), may fill 8 of the 16 waiting places by default
  (`DOCINT_WORKBENCH_SLOTS` times that share), and an abandoned call holds its
  slot and that user's turn until its wall time.
- An executor with no free slot, or one the api cannot reach, is a `503` too.
- So is a job the executor stopped because the container held more than
  64 MiB of shared memory that no job's count showed (`hidden_memory`): it
  stops every running job then, the innocent with the rest
  ([sandbox.md](sandbox.md#limits)). Nothing was wrong with the call; retry.
- A job's code gets 9/10 of `WORKBENCH_JOB_WALL_S` for `run_python` and 3/4 for
  the other tools, before the wall time stops it (see
  [sandbox.md](sandbox.md#limits)).
- The api waits for a job's answer up to the wall time plus 300 seconds
  (`workbench/api/jobs.py:_READ_GRACE_S`): the executor's cleanup of a killed
  job can take that long.
- Open WebUI sets no timeout on a tool call by default. From open-webui-service
  v0.14.0 its `docker/compose.yaml` sets one,
  `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER: "480"`. It must be longer than a call can
  take in workbench: `WORKBENCH_QUEUE_WAIT_S` + `WORKBENCH_JOB_WALL_S` + 300
  seconds, 480 seconds with the defaults (60 + 120 + 300). The 480 there is a
  fixed value, not derived from workbench's settings: raising either of the two
  here means raising it there too. With a shorter one, Open WebUI gives up on
  a call whose job is slow to be cleaned up before workbench answers it, and the
  model never gets the answer, although workbench may still finish the job,
  record it and keep its files.

## Files

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_UPLOAD_MAX_MB` | 50 | Largest upload, per file, in MB. A larger one answers `413`. A request whose body passes this plus 1 MiB is refused while it arrives (`workbench/api/body_limit.py`). It also bounds each file a job writes and a job's outputs together. |
| `WORKBENCH_WORKSPACE_MAX_MB` | 500 | Largest workspace per user, all chats together, uploads and the files tools made, in MB. An upload that would pass it answers `507`; a tool's new file that would pass it is not kept, and the answer says so (`quota`). A full disk also answers `507`. It also bounds what a running job may write (see [sandbox.md](sandbox.md#limits)). |

### Request bodies

Every other request is held to 2 MiB of body: a larger one answers `413`
`{"detail": "request too large"}` while it arrives and is never parsed
(`workbench/api/body_limit.py:BodyLimit`). A tool request could not pass the
executor's 1 MiB line anyway. A request without a valid caller key answers
`401` before any of its body is read, whatever its path
(`workbench/api/auth.py:KeyGate`); only `/healthz` and `/metrics` need no key.

## The api container

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_API_MEM_LIMIT` | `2g` | Memory of the api container, swap included (`mem_limit` and `memswap_limit`). |
| `WORKBENCH_API_PIDS_LIMIT` | 128 | Processes and threads of the api container (`pids_limit`). |

- **Memory:** the api's `/tmp` is a tmpfs of 512 MiB that holds uploads while
  they arrive, and counts against the limit; the rest is the process, which
  holds the requests in hand (each at most 2 MiB of body, or one upload). Past
  the limit the kernel stops the api, and `restart: unless-stopped` brings it
  back; the host's other services keep their memory.
- **Processes:** the api runs one process (`--workers 1`) with 40 threads for
  the routes' blocking work, up to 32 for the queue's and the sweep's, and a
  few more: about 80 in all.

## The executor container

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_EXECUTOR_MEM_LIMIT` | `6656m` | Memory of the executor container, swap included (`mem_limit` and `memswap_limit`). |
| `WORKBENCH_EXECUTOR_PIDS_LIMIT` | 320 | Processes of the executor container (`pids_limit`). |
| `WORKBENCH_EXECUTOR_CPUS` | 4 | CPUs the executor container may use, all its jobs together (`cpus`). |

The formulas, for other slot counts or job memory:

- **Memory:** `WORKBENCH_EXECUTOR_SLOTS` × `WORKBENCH_JOB_MEMORY_MB` + 512 MiB
  for the supervisor. The default is 4 × 1536 + 512 = 6656 MiB. The executor
  refuses to start when its cgroup limit is below slots × job memory.
- **Processes:** 64 per slot (each job's `RLIMIT_NPROC`) plus 64 for the
  supervisor and its cleanup helpers: 4 × 64 + 64 = 320.
- **CPUs:** Docker refuses to start a container whose `cpus` is above the
  host's number of CPUs. On a host with fewer than 4 CPUs, set
  `WORKBENCH_EXECUTOR_CPUS` to the host's number or less. CI does so on small
  runners.
- **cgroup v2:** the executor needs a cgroup namespace of its own and a
  `memory.stat` that gives `shmem` (Docker's default on cgroup v2). A host on
  cgroup v1, `cgroupns: host`, or a rootless daemon without the memory
  controller delegated cannot run it: it refuses to start
  ([sandbox.md](sandbox.md#what-the-supervisor-checks-before-it-starts)).
- **Free space on the volume:** not a setting. The executor keeps 1 GiB free
  on the filesystem that holds `workbench-data`, or 5 % of that filesystem where
  that is more: below it, every running job is stopped and no job starts, and
  the tool answers `507` ([sandbox.md](sandbox.md#limits)). For a local named
  volume that filesystem is the disk of Docker's data root, which images,
  containers' logs and other volumes share: their growth stops jobs too, and on
  a large disk the floor is large (100 GB of a 2 TB disk). Size that disk for
  the users' quotas, everything else on it, and the floor.

## Audit and retention

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_RETENTION_DAYS` | 7 | Days a workspace file or job record is kept after it was made. A changed value applies at once, with no notice ([retention.md](retention.md#a-changed-period-applies-at-once)). |
| `WORKBENCH_RETENTION_INTERVAL_S` | 300 | Seconds between retention sweeps. |
| `AUDIT_REQUIRE_PURPOSE` | `query,disclose,combine` | Actions a call must state a purpose for (`X-Audit-Purpose`), on the MCP door only; Open WebUI cannot state one. An empty value set in `.env` requires none. An unknown action stops the start. |

The period is shared with open-webui-service: from v0.14.0 a banner there tells
every user how long workspace files are kept, and deploy's `make env` copies
`WORKBENCH_RETENTION_DAYS` from workbench's `.env` to open-webui-service's
(`[shared: open-webui-service]` in `.env.example`). Set it in workbench's `.env`:
a value set only in open-webui-service's would be copied into workbench's, and
workbench would delete by it, with no notice.

`AUDIT_OUTPUT` is set by compose (`container`) and is not in `.env.example`
([audit.md](audit.md#settings)).

## Development only

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_HOST_PORT` | 8100 | Host port of the api in the dev overlay (`make up-dev`), on `127.0.0.1`. |

### The `compose.override.yaml` gotcha

`docker/compose.override.yaml` publishes the api's port 8000 on
`WORKBENCH_HOST_PORT` of the host's loopback, `127.0.0.1` only: `/healthz` and
`/metrics` need no key. `make up-dev` layers it on purpose; `make up` runs
`docker/compose.yaml` alone, the production shape with no host port. A bare
`docker compose` run from `docker/` loads `compose.override.yaml` on its own,
since that is compose's default override file name, and so publishes
`127.0.0.1:8100`, without the root `.env` either. Use `make`, or
`docker compose --env-file .env -f docker/compose.yaml` from the repo root.

## Not exposed

These are read by the code but not offered in `.env` or compose. Each is tied to
the images or the mounts, and changing it alone would break the stack
(`tests/test_compose_defaults.py` keeps them out):

| Variable | Fixed at | Why |
|---|---|---|
| `WORKBENCH_DATA_DIR` | `/data` | the mount point of `workbench-data` in every service |
| `WORKBENCH_EXECUTOR_SOCKET` | `/run/workbench/executor.sock` | the socket in `workbench-run`, which both services mount there and the executor's healthcheck names |
| `WORKBENCH_SLOT_UID_BASE` | `20001` | the slot users the executor image creates, `20001` to `20016` |

`WORKBENCH_VERSION`, the image tag, comes from `make` (the vendored
`make/common.mk`), not from `.env`.
