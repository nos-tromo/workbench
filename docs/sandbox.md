# Sandbox

Every file a user uploads is opened in the `executor`, never in the api: the
document readers, the search, the template work, the writers and the model's
free Python. This page says how the executor is confined, why it holds each
capability it has, what it checks before it starts, and the risks that remain.

## The executor container

`docker/compose.yaml`, service `executor`:

- `network_mode: none`: no network interface but its own loopback. The api
  reaches it over a Unix socket in the project-local volume `workbench-run`
  (mode `0660`, the api's group; its folder is root's alone).
- `read_only: true`, `no-new-privileges`, `cap_drop: [ALL]` and six
  capabilities added back (below).
- `user: "0:0"`: the supervisor runs as root, so that it can run each job as
  another uid. No job runs as root.
- `init: true`: an init reaps the processes a job leaves behind.
- tmpfs `/tmp` (`mode=0700`, root's alone) and `/dev/shm` (`mode=0755`, root's
  alone, 1 MiB).
- sysctls `fs.mqueue.queues_max: 0` (no POSIX message queue can be made) and
  `kernel.shm_rmid_forced: 1` (a SysV shared memory segment dies once nothing is
  attached to it).
- `pids_limit`, `mem_limit` equal to `memswap_limit`, and `cpus` (see
  [Limits](#limits)).
- No secret: the executor never reads the caller key, and compose gives it
  none.

## Capabilities

### The executor's six

| Capability | Why |
|---|---|
| `SETUID`, `SETGID` | The job's process, between `fork` and `exec`, drops every group and becomes its slot's uid and gid for good, and checks that no other id is left (`workbench/executor/sandbox.py:preexec`, `_drop_to`). The cleanup helpers drop to the slot the same way. |
| `CHOWN` | It gives the job's folder the slot's group, hands `out/` and `home/` to the slot, and the staged outputs to the api (`workbench/executor/fs.py:settle`). |
| `FOWNER` | It sets the modes of what it has just handed to another uid, and of folders it does not own. |
| `DAC_OVERRIDE` | It reaches into folders other uids own: it copies the inputs out of the api's `0700` workspaces, makes the job's folder in the api's `0700` staging, reads the result out of the slot's `0700` `home/`, measures the slot's folders while the job runs, and removes them after the job. |
| `KILL` | It signals the job's process group, which runs as the slot's uid, when the job ends or runs out of time (`sandbox.py:_kill_group`). Every other process of the slot is killed by a helper that is the slot itself. |

The supervisor refuses to start as anything but root
(`workbench/executor/supervisor.py:startup_problem`).

### `volume-permissions`' three

A one-shot service runs `workbench.executor.prepare_volume` as root before the
api and the executor start, with no network and three capabilities:

| Capability | Why |
|---|---|
| `CHOWN` | It hands each part of the layout to its owner. |
| `FOWNER` | It sets the modes of the parts it does not own, such as the api's `workspaces/` on a later run. |
| `DAC_READ_SEARCH` | It opens the api's `0700` folders and `0600` database, read-only, to change their owner and mode through the open descriptor. |

`DAC_OVERRIDE` is not needed there. It never writes through another uid's
permissions: it makes new parts only in the volume root, which is root's, and
changes owners and modes through descriptors, which `CHOWN` and `FOWNER`
cover.

## Isolation

### The uid pool and the slot lifecycle

The executor runs `WORKBENCH_EXECUTOR_SLOTS` jobs at once (4 by default, at
most 16). The slots are numbered from 0, as the supervisor's log names them
("slot 0 retired"). Slot `i` runs as uid and gid `20001 + i`, which is the
image's user `slot<i+1>`: slot 0 is user `slot1`, uid `20001`, and slot 15 is
user `slot16`, uid `20016`. None of these users can log in or owns a home
(`docker/Dockerfile.executor`). The pool never holds root or the api's uid
`10001` (`supervisor.py:make_slots`).

One job runs in a slot at a time:

1. The supervisor makes the job's folder `jobs/<job-id>/` and copies the
   requested files of the chat folder into it (below).
2. It runs `python -I -m workbench.executor.job <spec> <result>` as a fresh
   process in that folder, as the slot's uid, with the limits below
   (`sandbox.py:run_in_slot`). The process leads its own session, has a clean
   environment (`sandbox.py:CLEAN_ENV`, with `HOME` and `TMPDIR` in the job's
   own `home/`) and a `077` umask; its stdin, stdout and stderr are
   `/dev/null`. Before it drops to the slot's uid it raises its OOM score and
   installs the seccomp filter that keeps it from sockets and from memory the
   watchdog could not count (below).
3. While it runs, a watchdog measures it about twice a second (below). When
   the job ends, by itself, on the wall clock, on the watchdog or because the
   request was cancelled, its process group is killed. Then every process of the slot's uid
   is killed: a helper that drops to the slot calls `kill(-1, SIGKILL)`, and
   `/proc` is checked, up to six times with a growing pause
   (`sandbox.py:kill_slot_processes`).
4. Then whatever IPC the slot left is removed, as the slot: SysV shared memory
   (emptied first), message queues and semaphores it owns or made, and its
   entries in `/dev/shm` and `/dev/mqueue`. A second look must find nothing.
5. Only then is the result read and the outputs staged, and the job's folder
   removed. The slot is free again once its folder is gone.

A slot whose processes or IPC could not be cleared, whose folder could not be
removed, or whose job was cancelled in the middle, is retired for the life of
the process. Its next job could otherwise share a uid with what is left. Once
the last slot is retired the supervisor exits non-zero, and
`restart: unless-stopped` brings it back with fresh slots, a fresh IPC
namespace and a fresh `/dev/shm`. A job folder whose removal failed is root's
alone (`0700`) from then on, and the supervisor tries to remove it again every
five minutes (`supervisor.py:Supervisor._retry_leftovers`); the log says how
many of how many went. The removal runs in a thread, never on the event loop
that times the other jobs.

### What a job sees

- `jobs/<job-id>/`, root's, `0710`, its group the slot's: the job may pass
  through it and nobody else may. The job cannot list it, add a name to it or
  change its mode, so it cannot open it to another slot. Nobody may list
  `jobs/` itself (root's, `0711`). `spec.json` in it is root's, `0444`.
- `workspace/<chat-id>/` below it, root's, `0755`: the job's working folder,
  with root-owned `0444` copies of its input files. The job can read them and
  change nothing.
- `workspace/<chat-id>/out/`, the slot's, `0700`: the only place its outputs
  go.
- `home/`, the slot's, `0700`: its `HOME`, where it writes `result.json`, with
  `home/tmp/`, its `TMPDIR`.
- `/workspace` in the image is a link to `/proc/self/cwd/..`, so
  `/workspace/<chat-id>/<name>` is the job's own copy, whichever job asks.

`out/` and `home/` are all a job can write. A job may open them to everyone;
they stay out of reach all the same, behind the job folder only its slot can
pass. A job cannot reach the users' files, the staging folder, the database,
the socket or another job's folder: each is another uid's and closed to others.

### No sockets, no unseen shared memory

Every job runs under a seccomp filter of workbench's own
(`workbench/executor/seccomp.py`), on top of Docker's default profile. It is
installed in `preexec`, while the child is still root and after
`no_new_privs`, and every process the job starts inherits it:

- `socket()` fails with `EACCES`, whatever the family: Internet (the
  container's loopback), Unix (abstract names) and netlink among them;
- `socketpair()` works for `AF_UNIX`, which asyncio's self-pipe uses; the pair
  reaches only the job's own processes;
- `bind()` fails, and so does `setsockopt` with `SO_PASSCRED`, `SO_PASSPIDFD`
  or `SO_PASSPIDFD_THREAD`, which would make a Unix socket name itself: no
  socket of a job ever has a name another job could reach;
- `io_uring_setup()` fails (an io_uring can make sockets past the filter);
- `memfd_create()`, `memfd_secret()` and `mmap()` with both `MAP_SHARED` and
  `MAP_ANONYMOUS` in its flags fail. Shared anonymous memory can be filled,
  left mapped in a child the process forks and unmapped by the process itself:
  `fork` copies no page of a shared mapping into the child's page tables, so
  those pages are then in no process's resident set, where the watchdog counts
  a job's memory. A private anonymous map and a shared map of a file work.

At start the supervisor also takes the write bits of group and others off
`/dev/zero` (`supervisor.py:_close_dev_zero`; Docker's `/dev` is the
container's own tmpfs, and root owns the node). A shared map of `/dev/zero`
opened for writing is shared anonymous memory too; opened read-only, it can
only be read and holds no memory. It refuses to start if the node stays
writable by others.

It knows x86_64 and aarch64, each with its own syscall numbers; a call of
another architecture fails the same way, and the supervisor refuses to start on
another machine. pandas, numpy, matplotlib, openpyxl, python-docx, threads,
asyncio and ctypes callbacks work under it
(`tests/executor/test_supervisor.py::test_as_root_a_job_has_no_socket_and_its_libraries_still_work`).
Nothing needs a socket or shared anonymous memory in a job: the executor has no
network, and multiprocessing, the one user of shared anonymous memory among the
image's libraries, does not work there anyway.
The files of the user's other chats are never copied in. A file below a folder
named `out` is never copied in either: `out/` is where a job writes.

### Limits

Per job (`workbench/protocol.py:Limits`, set by `sandbox.py:preexec`). The
supervisor caps whatever a request asks at its own settings
(`supervisor.py:Supervisor._bounded`):

| Limit | Value |
|---|---|
| Wall time | `WORKBENCH_JOB_WALL_S` (120 s); then the group is killed and the answer is a timeout |
| The code's own time | 9/10 of the wall time for `run_python`, 3/4 for every other tool, stopped by `SIGALRM` inside the job (`workbench/executor/timer.py`); the wall time is the backstop |
| `RLIMIT_AS` | `WORKBENCH_JOB_MEMORY_MB` (1536 MB) of address space, per process |
| Memory of the job | `WORKBENCH_JOB_MEMORY_MB` for all its processes and its SysV segments together, measured by the watchdog (below) |
| `RLIMIT_CPU` | `WORKBENCH_JOB_WALL_S` seconds of CPU |
| `RLIMIT_FSIZE` | `WORKBENCH_UPLOAD_MAX_MB` (50 MB) per file written |
| What the job writes | `WORKBENCH_WORKSPACE_MAX_MB` (500 MB) in `home/` and `out/` together, at most 10 000 files and folders, nested at most 256 deep, measured by the watchdog |
| `RLIMIT_NPROC` | 64 processes of the slot's uid |
| `RLIMIT_NOFILE` | 256 open files |
| `RLIMIT_CORE` | 0 |
| Priority | niceness 10 or more, which a job cannot lower again (`sandbox.py:JOB_NICENESS`) |
| OOM score | `oom_score_adj` 1000 (`sandbox.py:JOB_OOM_SCORE_ADJ`): the kernel's OOM killer picks a job's process first; a process of the slot below it is a broken limit (below) |

The watchdog (`supervisor.py:_Watchdog`) measures every running job about
twice a second, in one thread of its own. Each pass looks once at `/proc` and
the SysV table for all slots (`sandbox.py:slots_use`), then walks each job's
`home/` and `out/` by descriptor without following a link, one job after the
other. Per slot it counts:

- the resident memory of every process of the slot's uid, without its shared
  memory (`VmRSS` minus `RssShmem`);
- every SysV shared memory segment the uid made, once, by the pages it holds
  (its resident and swapped bytes), whether a process has it attached or not;
- whether a process of the uid runs with an `oom_score_adj` below 1000
  (`/proc/<pid>/oom_score_adj` is world-readable);
- the disk, entries and depth of `home/` and `out/`.

Past a limit it stops the job: its process group is killed, the slot's cleanup
kills the rest, and the answer names the limit ("the job was stopped: it wrote
more than 500 MB", "…: one of its processes lowered its OOM score"). The wall
clock is kept on the event loop and waits on no thread, and a pass never waits
on the loop's own pool, which copies the inputs and stages the outputs. A
measure that fails stops the job and retires its slot ("the job was stopped:
its use of memory and disk could not be measured"); the log names the error's
type and the slot. A walk holds one descriptor per level, and the removal of
every slot's folder may walk at once, so the supervisor raises its own soft
limit of open files to the hard one at start and refuses to start below
slots × 258 + 256 (the jobs keep theirs).

What the memory limit holds: a job's processes together, and every SysV
segment it made, within its share of the container, between two measures.
The shared memory no resident set counts is closed to a job (above), so what
it holds is counted. What it does not count is in
[Residual risks](#residual-risks).

For the container (`docker/compose.yaml`, see
[configuration.md](configuration.md#the-executor-container)):

- `cpus`: `WORKBENCH_EXECUTOR_CPUS` (4), every job together;
- `pids_limit`: `WORKBENCH_EXECUTOR_PIDS_LIMIT` (320): 64 per slot plus 64
  for the supervisor and its cleanup helpers;
- `mem_limit` and `memswap_limit`, the same value:
  `WORKBENCH_EXECUTOR_MEM_LIMIT` (6656 MiB): every slot's job memory plus
  512 MiB for the supervisor. Memory and swap together are that one limit, so
  no job swaps past it. The watchdog holds each job to its share; what a job
  takes between two of its measures, and the kernel memory no measure counts
  (below), is held by this limit, and the OOM score makes a job's process the
  one the kernel stops.

Documents get guards of their own before a library parses them: an Office file
may unpack to at most 20 times the upload limit, with at most 10 000 members
(`workbench/executor/readers/__init__.py:check_zip`), and a workbook's sheets,
names and shared strings are counted before it is loaded.

### What the supervisor checks before it starts

`supervisor.py:startup_problem` refuses to start, with a one-line reason,
unless:

- it runs as root;
- the slot pool is sound: at most 16 slots, uids between 1000 and 65533, not the
  api's;
- every slot uid is a user of the image;
- the container's cgroup memory limit (`memory.max`) holds every slot's job
  memory. Without a cgroup v2 limit to read, this check passes; compose's
  limit still applies;
- `fs.protected_hardlinks` and `fs.protected_symlinks` read 1. These are the
  host's kernel settings. The first keeps a job from giving `out/` a second name
  for a file it does not own, such as the database, which root would then copy
  out; the second keeps anyone from following a link a job left;
- `/dev/mqueue` is a mounted mqueue file system, so a slot's queues can be
  found and removed;
- `/dev/shm` is root's and neither group nor others may write it, and
  `fs.mqueue.queues_max` reads 0. No slot can then leave anything in POSIX
  shared memory, semaphores or queues;
- `/tmp` is root's and neither group nor others may write it: every job has a
  `TMPDIR` of its own, so `/tmp` would be the last folder two slots could share;
- `/proc/self/status` shows `Seccomp: 2`, a seccomp filter such as Docker's
  default profile. It refuses the kernel keyring calls, and a keyring a job
  filled would outlive the job;
- `/proc/self/status` shows `NoNewPrivs: 1`. It keeps a setuid or setgid
  program inert for a job; the image has none either
  (`docker/Dockerfile.executor` strips the bits);
- the cgroup's process limit (`pids.max`), when there is one, holds every
  slot's 64 processes and 64 more for the supervisor and its helpers;
- the machine is x86_64 or aarch64, and a child under the jobs' seccomp filter
  can neither open an Internet socket nor map shared anonymous memory, and can
  still make a Unix pair and a private anonymous map (`seccomp.py:problem`).

Then it makes two changes to its own container and starts only if they hold
(`supervisor.py:prepare_host`): its soft limit of open files goes up to the hard
one, which must hold slots × 258 + 256 descriptors; and `/dev/zero` is closed
to others' writes (`0644`).

Compose sets the container up this way. `seccomp=unconfined`, a missing
`no-new-privileges` or Docker's default `/dev/shm` make it refuse.

### Output validation

After the job, before anything is staged (`supervisor.py:_find_outputs`,
`_stage_one`):

- `result.json` must be a regular file of the slot's uid with exactly one
  link, as an output must, read without following a link or blocking on a
  FIFO, and bounded in size.
- Only regular files directly in `out/` count. Links, folders and anything
  else are left out, and so are names a workspace refuses.
- Each must be the slot's own: owned by the slot's uid, with exactly one link.
  A file with a second name could be another uid's file, linked in. Others are
  dropped, and the answer counts them.
- At most 20 files, and together at most `WORKBENCH_UPLOAD_MAX_MB`. Past either
  bound no file is kept, and the answer says why. Each file is also bounded by
  `RLIMIT_FSIZE` while it is written.
- Each file is opened again without following a link and must be the same file
  (device and inode) as when it was found; it is copied to
  `staging/<job-id>/`, handed to the api and given mode `0600`.

The api records the job by the ids its outputs will have, then moves each
staged file into the chat folder under a free name
(`workbench/api/store.py:Store.register_output`). Outputs count against the
user's quota (`WORKBENCH_WORKSPACE_MAX_MB`) like uploads. What it cannot keep
(the quota, a full disk, an invalid name) is removed with the staging folder,
counted in the answer and recorded again as an error.

### The healthcheck

The executor is healthy once its socket listens: the probe looks for the
socket's path in `/proc/net/unix` with the listening flag, `00010000`
(`docker/compose.yaml`, `healthcheck`). The socket file alone does not count:
it outlives a supervisor that was killed, in `workbench-run`. The probe never
connects, since a connection without a request line would be a malformed
request. The api starts only once the executor is healthy.

## Residual risks

Each of these is accepted. Each says what it would take.

- **A shared kernel.** The jobs run in containers, on the host's kernel. A job
  that exploits a kernel flaw reachable from an unprivileged uid with no
  capabilities, no network and Docker's default seccomp profile could escape
  to the host. That takes a kernel vulnerability of that kind. The federation is
  single-host Docker; gVisor (below) narrows this where a host has it.
- **A channel between two jobs that run at once.** Two jobs of two users that
  cooperate could pass a user's files between them while both run, for example
  a listener of one user and a sender in another user's chat, put there by a
  prompt hidden in a document. The full-bandwidth ways are closed: the
  container's loopback, abstract Unix sockets and netlink (no job has a
  socket, below), and the job folders (root's, closed to every other slot). What
  remains: SysV IPC while both run (a segment, a queue or a semaphore one job
  makes and hands to another uid; each is removed with its creator's job), the
  `/proc` entries of the other slot's processes (their command lines name a job
  id, and their status), and CPU and timing. Nothing survives the end of a job:
  each slot is cleared before its next one.
- **What the watchdog cannot see between two of its measures.** It measures
  about twice a second. A job can write, between two measures, as much as the
  disk takes in half a second, and take as much memory as it can map in half a
  second, or lower its OOM score for that long; the container's memory limit and
  the job's OOM score hold the memory.
  A file a job has removed but keeps open is in no folder the watchdog walks: up
  to 256 such files (`RLIMIT_NOFILE`) of 50 MB each (`RLIMIT_FSIZE`), freed
  when the job ends. A job that renames its folders while they are walked can
  be counted short by one measure.
- **What the memory sum does not count.** The kernel's own memory that a job's
  processes cause (page tables, pipe and socket-pair buffers, the inodes of its
  files) and the page cache of the files it reads and writes are in no resident
  set. They are bounded by the job's other limits (processes, open files, file
  size, its folders' disk and entries) and held, with the rest, by the
  container's memory limit and the job's OOM score. A process's resident set
  counts pages it shares with its forked children once in each, which can stop
  a job early, never late.
- **A SysV segment handed between two running jobs.** A segment counts against
  the slot that made it. A job can open one it made to every uid; another job
  that runs at once can attach it and keep it attached after the first job
  ended (the slot's cleanup empties and removes it, but the attached mapping
  stays). What the second job then writes into it counts against the first
  slot's next job, and against no job while that slot is idle, so it is held
  only by the container's memory limit. It takes two jobs of two users that run
  at once and cooperate, as the channel above does; it is a denial of service
  against concurrent jobs, and it reaches no user's files.
- **A job can have its own slot retired.** A job that leaves its folder so it
  cannot be removed (for example a tree deeper than the supervisor's open
  files allow) or wins the race against its slot's cleanup gets its slot
  retired; once every slot is, the executor restarts, and the jobs of other
  users in it fail. It is a denial of service within the user's own rate
  limits, bounded and self-healing.
- **`fs.protected_regular` and `fs.protected_fifos` are not required.** They are
  the host's settings, not namespaced, and 0 on many Docker hosts, so requiring
  them would refuse most hosts. They guard opens in world-writable sticky
  folders. The executor has none a slot can write: `/tmp` and `/dev/shm` are
  root's, and each job's `TMPDIR` is its own. It would take such a folder, which
  the start-up checks rule out.
- **The root-side look at the IPC folders checks only their top.** After the
  helper's cleanup, the supervisor looks for the slot's entries at the top of
  `/dev/shm` and `/dev/mqueue`, and one level into folders of other uids there,
  within a budget (`sandbox.py:_folder_holds`). An entry a job put deeper inside
  another uid's folder can be missed. In production no slot can write either
  folder, so the look finds nothing and is defence in depth. It would take
  Docker's default `/dev/shm`, which the start-up checks refuse, and two jobs
  that cooperate.
- **userns-remap is untested.** The executor has not been run on a host whose
  Docker daemon remaps user namespaces (deploy `docs/runbooks/userns-remap.md`).

## gVisor

`runtime: runsc` on the `executor` service runs its jobs on gVisor's user-space
kernel, which narrows the shared-kernel risk. It is an optional hardening for
hosts that have gVisor installed, and it is untested: the start-up checks read
`/proc` and `/sys` values that gVisor emulates, so try it on the host before
relying on it.

## Limits of `run_python`

The model's code runs in the job process itself, as a script
(`workbench/executor/runners/python.py:run_python`). What follows from that:

- **Only Python-level output is captured.** `print` and writes to `sys.stdout`
  and `sys.stderr` come back. Writes straight to file descriptors 1 and 2 (a C
  extension, `os.system`, a child process) go to `/dev/null`.
- **`os.chdir` breaks `/workspace/<chat>/x` paths.** `/workspace` is a link to
  the parent of the current folder, so after `os.chdir` those paths point
  elsewhere. Relative paths in the job's own folder keep working until the code
  changes folder; the runner puts the folder back after the code.
- **A lingering non-daemon thread ends the job as a wall timeout.** The process
  waits for such a thread before it exits. The job then runs into the wall time
  and is answered as a timeout, even though its result was already written.
- **No multiprocessing.** No slot can write `/dev/shm` or make a POSIX queue, so
  `multiprocessing`, its process pools and the libraries built on them (such as
  joblib's process backend) fail. Threads work. The tool description and the
  system prompt say so.
- **Path rewriting depends on the supervisor's layout.** The answer never shows
  the job's real folder: paths below `jobs/<job-id>/workspace/<chat>` are written
  as `/workspace/<chat>/…`, the rest of the job's folder as `<job>`, other paths
  on the volume as `<data>` (`python.py:_Rewriter`). The rewriting is textual
  and built from that layout; a change to the layout must change it too.
