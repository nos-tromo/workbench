# Retention

workbench deletes every file `WORKBENCH_RETENTION_DAYS` days after it was
made, 7 by default, and records each deletion before it happens. The sweep runs
inside the api (`workbench/api/retention.py:Sweeper`), at start and then every
`WORKBENCH_RETENTION_INTERVAL_S` seconds (300), in a thread off the event loop.
It also removes what interrupted work left behind, and forgets old job records.

## The rules

### Files past the period

- **Due by creation.** A file is due once its `created_at` lies more than the
  period back: an upload or an output, whether or not its chat still exists in
  its caller. Viewing or using a file never extends it, and
  there is no extension of any kind (`workbench/api/store.py:Store.due`).
- **In pages and batches.** A sweep reads the due files oldest first, in pages
  of at most 200, and deletes them in batches of one user each
  (`retention.py:_batches`).
- **Record first, then remove.** Each batch is first recorded as one `delete` /
  `retention.sweep` event of the system, by file id, with the period, the cutoff,
  the caller, the owner (the user's opaque id) and the count. Only then do its
  files and their rows go (`Sweeper._delete`). [audit.md](audit.md#retention-events)
  has the event.
- **An error record for a failed removal.** A file that cannot be deleted keeps
  its row, is recorded again with `outcome: error`, and is tried again by the
  next sweep.
- **Paging past failures.** The sweep goes on past the files it could not
  delete, so no number of them holds up the others (`Store.due`, `after`).

### No deletion without its record

- A record that cannot be written ends the api process (`os._exit(1)`) before
  anything more is deleted (`Sweeper._halt`). stderr says how many files the
  record was about, and `restart: unless-stopped` brings the api back. Nothing
  is deleted until it can record again.
- The other way round is allowed. A sweep stopped between a batch's record and
  the end of its removals (the api shuts down, or is killed) leaves the rest of
  the batch to the next sweep, which records those files again. A double record
  is acceptable; a deletion without a record is not.
- On shutdown the sweep in hand stops before its next record or removal, and
  only then does the audit chain end (`Sweeper.run_forever`).

### Leftovers of interrupted work, after an hour

Every regular file these rules remove is recorded first too, on the same
halt, once it has been untouched for an hour (`retention.py:FOLDER_GRACE_S`):

- **Staging residue.** `staging/<job-id>/` holds outputs the api never
  registered: the client went away, or the api stopped before the job settled.
  They were made from the user's files. The folder's regular files are recorded
  (`rule: staging`, the job's id, the count, and the job's caller, owner and chat
  while its record exists), then the folder goes (`Sweeper._clear_staging`). An
  empty staging folder goes without a record; a folder whose name is not a job
  id is left alone.
- **Orphans.** A regular file in a chat folder that has no row: the
  `<file-id>.part` of an upload the api died during, or a file a kill left
  between its rename into place and its row. They are recorded per chat folder,
  at most 200 to a record (`rule: orphan`, by the file ids their names carry,
  never a name), then removed (`Store.sweep_user`). The look and the removal
  hold the store's lock, which every rename into place holds until its row is
  committed, so a file on its way in is never taken for an orphan.

What could not be removed is recorded again with `outcome: error`.

### Without a record

These hold no content, so they go without an event:

- **Empty folders.** Folders inside chat folders, chat folders and a user's
  root go once they are empty and have been untouched for an hour, under the
  store's lock, so a file on its way into one never loses it
  (`Store.sweep_user`). `workspaces/` and a caller's folder never go.
- **Job records.** A job's record (its tool, chat, start time and status) is
  forgotten once its start lies more than the period back
  (`Sweeper._forget_jobs`).

Nothing is followed: a link, anything that is neither a folder nor a regular
file, and anything on another filesystem is left alone. An entry that cannot be
looked at is skipped with a warning, and the sweep goes on with the next.

### In the executor

A job's folder, with the copies of the user's files the job was given, goes
when the job ends. It is not the sweep's: the executor removes it
(`workbench/executor/supervisor.py:Supervisor._remove`). One whose removal
failed is closed to every slot, retried every five minutes until it goes
(`Supervisor._retry_leftovers`, the log says how many of how many went), and
removed at the latest when the executor restarts. Its files have no rows and
are not recorded: they are copies the job's own event covers.

### After a sweep

- **A checkpoint.** The database deletes with `secure_delete`, which overwrites
  the deleted rows, but its write-ahead log can still hold earlier versions of
  them, file names among them. A sweep that deleted rows ends with a `TRUNCATE`
  checkpoint, which copies the log into the database file and empties it
  (`Store.checkpoint`). When a reader keeps the log busy past the busy timeout,
  the checkpoint is carried to the next sweep, whether or not that one deletes
  anything.
- **The metrics** are measured again: the number of files and the disk use.

## A changed period applies at once

The period is read when the api starts. After a restart with a new
`WORKBENCH_RETENTION_DAYS`, the next sweep applies it to every file, by its
creation time. There is no notice period.

- A **shorter** period deletes, at once, every file older than it, earlier than
  its users were told.
- A **longer** period keeps every file longer, those already there included.

Announce a change to the users before it takes effect.

## The dry run

```bash
make retention-dry-run            # the configured period
make retention-dry-run DAYS=3     # preview another period
```

It runs `python -m workbench.api.retention dry-run [--days N]` in the running
api container (`docker compose exec`), so the api must be up. It prints what the
period would delete now: counts and age ranges by kind (`upload`, `output`)
and in all, never a name, an id, a user or a chat (`retention.py:dry_run`). It
reads the database through a read-only connection and records, deletes and
changes nothing.

SQLite makes an empty `-wal` and `-shm` beside a database in WAL mode that no
other connection holds open, because a reader needs them. So a dry run while
the api holds no connection may leave those two empty files; the api's next
connection takes them over.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `WORKBENCH_RETENTION_DAYS` | 7 | Days a file or job record is kept after it was made |
| `WORKBENCH_RETENTION_INTERVAL_S` | 300 | Seconds between sweeps |

The hour for leftovers and empty folders is fixed
(`retention.py:FOLDER_GRACE_S`).

## `make nuke` is outside the trail

`make nuke` removes the whole volume `workbench-data`, every user's files and
the database, after you type `workbench` to confirm (`Makefile`). No audit event
records it: it is the operator's act on the host, with Docker, outside the api
and its chain. The host's own audit (deploy ADR 0003, "The host covers what no
service sees") is what can record that call.

## Copies outside workbench

Retention deletes workbench's files. It does not reach the copies elsewhere:
the tool results in Ghostwriter's and docint's chat history, kept for their own
retention, and what users downloaded. See
[data-flow.md](data-flow.md).
