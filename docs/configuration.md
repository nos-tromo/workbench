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
| `DOCINT_WORKBENCH_KEY` | none | The bearer key docint presents on its MCP door (`/mcp`). At least 32 characters once surrounding spaces are stripped. Unset, docint's door is off. Generate it with `openssl rand -hex 32`. |
| `DOCINT_WORKBENCH_TOOLS` | `run_python` | The tools docint may call, comma-separated; each needs an inline form. |
| `DOCINT_WORKBENCH_WALL_S` | 60 | Wall-clock seconds one docint job may run: from 5 to `WORKBENCH_JOB_WALL_S`. A call's `time_limit_s` is a strict integer in that range (`30.0` is refused). |
| `DOCINT_WORKBENCH_SLOTS` | 2 | Jobs docint may have running at once; fewer than `WORKBENCH_EXECUTOR_SLOTS`. |
| `GHOSTWRITER_WORKBENCH_KEY` | none | The bearer key Ghostwriter presents on its MCP door (`/mcp`) and seven file routes. The same rules; unset, Ghostwriter's door is off. |
| `GHOSTWRITER_WORKBENCH_TOOLS` | the ten workspace tools | The tools Ghostwriter may call, comma-separated; each a workspace tool. Since 0.7.0 the default includes `view_file`; a list set explicitly must add it, or Ghostwriter offers no images. |
| `GHOSTWRITER_WORKBENCH_WALL_S` | 120 | Wall-clock seconds one Ghostwriter job may run: from 10 to `WORKBENCH_JOB_WALL_S`. |
| `GHOSTWRITER_WORKBENCH_SLOTS` | 3 | Jobs Ghostwriter may have running at once; fewer than `WORKBENCH_EXECUTOR_SLOTS`. A prepare of a picture or a PDF takes one of them, an Office file's conversion another (its conversion and then its drawing, one after the other; on demand too), and the two run apart, so keep 3 or more: at 3, one is left for the tool calls of all Ghostwriter's users while both run. At 2 (an `.env` that pinned the old default), a conversion and a picture's prepare take both, and every Ghostwriter tool call answers `wait_timeout` after 5 s while they run; nothing warns at start. There is no variable for prepares or conversions of their own. Since 0.8.1 a host with `WORKBENCH_EXECUTOR_SLOTS` 3 and this unset refuses to start (3 is not fewer than 3): raise the executor's slots there, or set this to 2 with the cost above. The executor's memory is its own slots times a job's (below), whatever this is. |

The api needs at least one of the two keys, or it refuses to start; two
callers with the same key stop the start too.

### The caller key

`DOCINT_WORKBENCH_KEY` is shared with docint, and `GHOSTWRITER_WORKBENCH_KEY`
with Ghostwriter; each holds the value as its `WORKBENCH_API_KEY`. workbench's
`.env` and the caller's must both hold it; change it in both and restart both
stacks. It is not the inference router's key. deploy's `make env` copies it
between the two files:
each key has its row (`workbench-docint-key`, `workbench-ghostwriter-key`) in
deploy's `env-sync.conf`, the `[shared: … WORKBENCH_API_KEY]` tag in
`.env.example` names it, and `make env ENV_GENERATE=1` generates one where it
is unset in both.

The keys are compared in constant time (`workbench/api/auth.py`). They reach only
the api: the executor never reads them.

## Network

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_NET` | `workbench-net` | Name of the external network between the api and its callers; `make network` creates it. Change it only if you renamed the network. |

The api joins it as `workbench-api` on port 8000, exposed and never published.
Only callers (docint, Ghostwriter) and obs-plane's scraper join it.
The executor has no network.

## Jobs

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_EXECUTOR_SLOTS` | 4 | Jobs the executor runs at once, one slot uid each; at most 16, the size of the uid pool. |
| `WORKBENCH_QUEUE_MAX` | 16 | Jobs that may wait for a free slot. Past it a call is refused at once (`queue_full`, "busy, retry later"). |
| `WORKBENCH_JOBS_PER_USER` | 1 | Jobs one user may have **running** at once. Their further calls wait, and hold no slot while they wait. |
| `WORKBENCH_JOB_WALL_S` | 120 | Wall-clock seconds a job may run; also its CPU-time limit. |
| `WORKBENCH_JOB_MEMORY_MB` | 1536 | Address space one job may map, in MB. `WORKBENCH_EXECUTOR_MEM_LIMIT` must hold the slots times this. |
| `WORKBENCH_RESULT_MAX_KB` | 16 | Largest answer a tool returns to the model, in KB: 1 to 256. Longer answers are cut and say how to go on. It bounds the text only: the images of `view_file` and `read_document` come on top (below). |

An answer that shows images is larger than `WORKBENCH_RESULT_MAX_KB`: its
images are at most 2.5 MiB raw (`MAX_SHOWN_BYTES`), about 3.3 MiB as base64,
and its text goes twice (the text block and `structuredContent`), each up to
`WORKBENCH_RESULT_MAX_KB`, JSON-escaped. All of it must stay under the caller's
cap on an answer, Ghostwriter's 4 MiB. At the default of 16 KB the margin is
wide; at the most, 256 KB, it is about 150 KB, so raise the setting that far
only with a caller whose cap leaves the room.

How the queue decides (`workbench/api/jobs.py:JobQueue`); a refused call gets
an `isError` result whose `reason` says why:

- Each user also has a cap on **waiting** calls:
  `WORKBENCH_QUEUE_MAX / WORKBENCH_EXECUTOR_SLOTS`, rounded down, at least 1
  (4 by default). A user past it is refused at once (`queue_full`), and the
  other users are not affected: one user cannot fill every waiting place.
- docint's and Ghostwriter's calls wait at most 5 s for their turn
  (`MCP_QUEUE_WAIT_S`, fixed; then `wait_timeout`), may fill 8 (docint's) and
  12 (Ghostwriter's) of the 16 waiting places by default
  (`DOCINT_WORKBENCH_SLOTS` or `GHOSTWRITER_WORKBENCH_SLOTS` times that share),
  and an abandoned call holds its slot and that user's turn until its wall time.
  Each is held to its own share only, so at the defaults docint and Ghostwriter
  together can hold all 4 slots and all 16 waiting places. Since 0.8.1 the two
  shares overlap (3 + 2 slots of 4, 12 + 8 places of 16): while Ghostwriter
  holds all of its own, docint keeps 1 running slot and 4 waiting places, where
  it had 2 and 8.
- A prepare (`POST /files/prepare`, the render job of a picture or a PDF just
  attached) waits for its caller's prepare gate in place of its user's turn, up
  to 120 s (`PREPARE_QUEUE_WAIT_S`, fixed; then `503`, "busy, retry later"):
  one prepare of a caller runs at a time, over all its users and inside its
  slots, and at most 32 wait (`PREPARE_WAITING_MAX`), counted apart from
  `WORKBENCH_QUEUE_MAX` and every share, so prepares never fill the places the
  users' own calls need. A client's timeout for the route should cover the
  120 s, the job's wall and 30 s. The renders a view or a tool makes run on the
  user's own turn.
- An Office file's conversion and the drawing of its pages after it, two jobs
  in one turn, wait for the caller's **conversion gate** and then a slot
  instead, whether the prepare of an attachment runs them (up to its 120 s) or
  a view or a tool converts the file on demand (up to 120 s too,
  `SOURCE_LOCK_WAIT_S`, past its 5 s for a turn: it waits out another file's
  conversion, which ends within one job wall, as for a file's lock below): one
  conversion of a caller runs
  at a time, over all its users, apart from the prepare gate, so a picture
  attached while a deck converts is prepared at once, and a caller never
  converts two files at once. At most 32 conversions wait, counted as the
  prepares are and apart from them. A client's timeout for the prepare of an
  Office file should cover the 120 s, two walls and 30 s (390 s at the default
  wall).
- A view or a tool that needs the renders of a file whose renders another call
  is making (such as a deck its prepare is converting) waits for that call, up
  to 120 s (`SOURCE_LOCK_WAIT_S`, fixed; then `wait_timeout`), and then answers
  from what it made, with no job of its own. 120 s is one job wall at the
  default: as long as the call would have spent converting the file itself,
  and short of Ghostwriter's 150 s for a tool call. The wait holds no slot, no
  turn and no waiting place.
- A file the executor refused at its prepare or its conversion is refused
  again at once by every later prepare, view or read of its pages, with no job,
  once the api remembers the refusal (of up to 4096 files, `REFUSALS_MAX`, in
  memory alone, until the file goes or the api restarts:
  [data-flow.md](data-flow.md#how-long)). A refusal the file decides is
  remembered at once: a file that is damaged or not what its name says,
  password-protected, a deck with no slides, a macro-enabled deck or a slide
  show, one with a picture over 120 megapixels or more pictures than can be
  drawn within the memory limit (also when a conversion redone in parts runs
  out of its time), one whose PDF would pass 32 MB. Any other `unreadable_file`
  (a job that could not write inside its sandbox, a conversion that ended for
  no reason the file gives) is remembered only when it comes twice in a row for
  the file, a prepare that went well between them starting the count again. A
  timeout or a busy executor is never remembered. Uploading the file again
  makes a new file, which is tried afresh.
- A `read_document` call on a PDF or a deck can run the read, a deck's
  conversion to a PDF, the render of its pages or slides without text and, when
  strips are asked for, a strips job one after the other, each as long as a job
  may: a caller's timeout for it should allow for all of them. A view or a tool
  that converts a file on demand can wait for the file's lock (up to 120 s),
  then for the conversion gate and a slot (up to 120 s more), then run two jobs
  (the conversion and the drawing): a caller's timeout for it should allow for
  the two waits and two walls; Ghostwriter's 150 s covers the usual case, not
  the worst.
- An executor with no free slot, or one the api cannot reach, is refused too
  (`executor_busy`).
- So is a job the executor stopped because the container held more than
  64 MiB of shared memory that no job's count showed (`refused`, with the text
  `hidden_memory: …`): it stops every running job then, the innocent with the
  rest ([sandbox.md](sandbox.md#limits)). Nothing was wrong with the call; retry.
- A job's code gets 9/10 of `WORKBENCH_JOB_WALL_S` for `run_python` and 3/4 for
  the other tools, before the wall time stops it (see
  [sandbox.md](sandbox.md#limits)).
- The api waits for a job's answer up to the wall time plus 300 seconds
  (`workbench/api/jobs.py:_READ_GRACE_S`): the executor's cleanup of a killed
  job can take that long.

## Files

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_UPLOAD_MAX_MB` | 50 | Largest upload, per file, in MB. A larger one answers `413`. A request whose body passes this plus 1 MiB is refused while it arrives (`workbench/api/body_limit.py`). It also bounds each file a job writes and a job's outputs together. |
| `WORKBENCH_WORKSPACE_MAX_MB` | 500 | Largest workspace per user, all chats together, uploads, the files tools made and the renders of pictures, PDFs and Office files (with the PDF an Office file is converted to), in MB. An upload that would pass it answers `507`; a tool's new file that would pass it is not kept, and the answer says so (`quota`). A full disk also answers `507`. It also bounds what a running job may write (see [sandbox.md](sandbox.md#limits)). |

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
  the tool is refused (`refused`, with the text `storage_full: storage is full`;
  [sandbox.md](sandbox.md#limits)). For a local named volume that filesystem is
  the disk of Docker's data root, which images, containers' logs and other
  volumes share: their growth stops jobs too, and on a large disk the floor is
  large (100 GB of a 2 TB disk). Size that disk for the users' quotas,
  everything else on it, and the floor.

## Audit and retention

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_RETENTION_DAYS` | 7 | Days a workspace file or job record is kept after it was made. A changed value applies at once, with no notice ([retention.md](retention.md#a-changed-period-applies-at-once)). |
| `WORKBENCH_RETENTION_INTERVAL_S` | 300 | Seconds between retention sweeps. |
| `AUDIT_REQUIRE_PURPOSE` | `query,disclose,combine,modify,collect` | Actions a call must state a purpose for (`X-Audit-Purpose`), on the MCP door and Ghostwriter's file routes. An empty value set in `.env` requires none. An unknown action stops the start. |

The period is shared with Ghostwriter, which keeps tool previews until it:
deploy's `make env` copies `WORKBENCH_RETENTION_DAYS` from workbench's `.env` to
Ghostwriter's (`[shared: ghostwriter]` in `.env.example`). Set it in
workbench's `.env`: a value set only in Ghostwriter's would be copied into
workbench's, and workbench would delete by it, with no notice.

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
