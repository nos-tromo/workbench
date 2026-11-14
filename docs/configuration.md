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

## Required

| Variable | Default | Meaning |
|---|---|---|
| `OPEN_WEBUI_WORKBENCH_KEY` | none | The bearer key Open WebUI presents on every call. At least 32 characters once surrounding spaces are stripped, or the api refuses to start. Generate it with `openssl rand -hex 32`. |

### The caller key

`OPEN_WEBUI_WORKBENCH_KEY` is shared with open-webui-service, which presents
the same value on its terminal-server connection. Both `.env` files must hold
it; change it in both and restart both stacks. It is not Open WebUI's
forwarding secret and not the inference router's key. deploy's `make env`
copies shared values between members once the key has its row in deploy's
`env-sync.conf`; the `[shared: open-webui-service]` tag in `.env.example` comes
with that row.

The key is compared in constant time (`workbench/api/auth.py`). It reaches only
the api: the executor never reads it.

## Network

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_NET` | `workbench-net` | Name of the external network between the api and its callers; `make network` creates it. Change it only if you renamed the network. |

The api joins it as `workbench-api` on port 8000, exposed and never published.
Only callers (Open WebUI) and obs-plane's scraper join it. The executor has no
network.

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
- An executor with no free slot, or one the api cannot reach, is a `503` too.
- A job's code gets 9/10 of `WORKBENCH_JOB_WALL_S` for `run_python` and 3/4 for
  the other tools, before the wall time stops it (see
  [sandbox.md](sandbox.md#limits)).
- The api waits for a job's answer up to the wall time plus 300 seconds: the
  executor's cleanup of a killed job can take that long.
- Open WebUI sets no timeout on a tool call by default. open-webui-service sets
  one (`AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER`); keep it above
  `WORKBENCH_JOB_WALL_S` plus `WORKBENCH_QUEUE_WAIT_S`.

## Files

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_UPLOAD_MAX_MB` | 50 | Largest upload, per file, in MB. A larger one answers `413`. A request whose body passes this plus 1 MiB is refused while it arrives (`workbench/api/upload_limit.py`). It also bounds each file a job writes and a job's outputs together. |
| `WORKBENCH_WORKSPACE_MAX_MB` | 500 | Largest workspace per user, all chats together, in MB. An upload that would pass it answers `507`. Outputs are not held to it when they are made; they use up the room of later uploads. A full disk also answers `507`. |

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

## Audit and retention

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_RETENTION_DAYS` | 7 | Days a workspace file or job record is kept after it was made. A changed value applies at once, with no notice ([retention.md](retention.md#a-changed-period-applies-at-once)). |
| `WORKBENCH_RETENTION_INTERVAL_S` | 300 | Seconds between retention sweeps. |

`AUDIT_OUTPUT` is set by compose (`container`) and is not in `.env.example`
([audit.md](audit.md#settings)).

## Development only

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_HOST_PORT` | 8100 | Host port of the api in the dev overlay (`make up-dev`). |

### The `compose.override.yaml` gotcha

`docker/compose.override.yaml` publishes the api's port 8000 on
`WORKBENCH_HOST_PORT`, on every interface of the host. `make up-dev` layers it
on purpose; `make up` runs `docker/compose.yaml` alone, the production shape
with no host port. A bare `docker compose` run from `docker/` loads
`compose.override.yaml` on its own, since that is compose's default override
file name, and so publishes `:8100` on all interfaces, without the root `.env`
either. Use `make`, or `docker compose --env-file .env -f docker/compose.yaml`
from the repo root.

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
