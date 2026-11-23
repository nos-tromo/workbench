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

### Renders

A render, the picture, page or slide image workbench makes of a file, is a
derived file: as old as its source, in the user's quota, reachable through no
path. So is the PDF an Office file is converted to (role `pdf`, kept beside its
renders): it goes with them, in the same batches, and is counted as a render in
`derived`.

- **Renders go with their source, in its record.** A batch's event names the
  batch's file ids and then the ids of their renders (`Store.derived_for`);
  its criteria keep `count` as the number of files and add `derived`, the
  number of renders. Each file goes with the renders named for it, and only
  those (`Store.remove`, `Sweeper._delete`). Past 200 ids the rest goes on a
  continuation event ([audit.md](audit.md#long-lists-of-files)). A file that
  could not go is recorded again as an error, with its renders' ids.
- **Strays and due renders are swept with `rule: derived`.** After the files,
  the sweep reads every render that is due, oldest first, in pages of at most
  200: one whose source row is gone (a stray: it was made after its file's
  record was written, or its file was removed without it), and one made
  before the cutoff whose file stayed because it could not be deleted
  (`Store.due_derived`, `Sweeper._sweep_derived`). Each batch of one user is
  recorded first as `delete` / `retention.sweep` with `rule: derived`, the
  period, the cutoff, the caller, the owner and the count, by the renders' ids;
  then each render's file and row go (`Store.remove_derived`). One that cannot
  be deleted keeps its row, is recorded again as an error, and is passed over
  like a file. Renders count in the sweep's totals, as strays and as errors.
- **A render without a row is an orphan.** A file in `.derived/<source-id>/`
  that has no row (a kill between its rename into place and its row) is
  swept with the orphans below, named by the id its name carries.

### A clip's artifacts

The two raw artifacts Nextext made of a clip (JSON Lines and keyframes zip, roles
`nextext_jsonl` and `nextext_zip`), the transcript and the frames made of them
are derived files of the clip, as the renders above are, and follow the same rules:

- **They go with the clip, in its record.** They carry its creation time, so
  they are due with it; a batch's event names the clip's id and then their ids
  (counted in `derived`), and Ghostwriter's chat delete and its taking back of
  the clip name every one of them in their one event too.
- **A replaced artifact is recorded.** A second `POST /files/derived` of a role
  replaces the clip's artifact of that role, and with it the transcript and the
  frames made from the old one (a clip's retention clock is not restarted: the
  new rows carry the clip's creation time). One `modify` / `file.derived` event
  names the clip, the new artifact and each row replaced, before the new
  artifact is visible; the new row is inserted and the replaced rows deleted in
  one transaction, and the replaced files go just after it. A file that could
  not be deleted then has no row, and the orphan rule above removes and records
  it. A clip's prepare that
  makes again what an earlier prepare left without its facts replaces those rows
  the same way, in its own `file.prepare` event (`replaced`).
- **An interrupted artifact is an orphan.** An artifact is first written whole as
  `<id>.part` in the clip's folder of derived files, before anything is recorded
  or visible; one that a kill (or a server shutdown in the middle of a transfer)
  leaves is removed after an hour with the orphans, recorded by its id.

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
  between its rename into place and its row; a render without its row in
  `.derived/<source-id>/<id>.<ext>` is one too, and is named by that id. They
  are recorded per chat folder, at most 200 to a record (`rule: orphan`, by the
  file ids their names carry, never a name), then removed (`Store.sweep_user`).
  The look and the removal hold the store's lock, which every rename into place
  holds until its row is committed, so a file on its way in is never taken for
  an orphan.

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
period would delete now: counts and age ranges by kind (`upload`, `output`,
`derived`) and in all, never a name, an id, a user or a chat
(`retention.py:dry_run`). The `derived` line counts renders: the due ones
(made before the cutoff, or strays, whatever their age) and the kept. It
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
