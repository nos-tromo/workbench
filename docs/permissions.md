# Permissions

What each tool and each file route of workbench may read and write, what it
records in the audit log, and what it keeps. Then the boundaries that hold for
all of them, and what the model is told.

workbench serves two callers, docint and Ghostwriter (see [Callers and
doors](#callers-and-doors)). Both call the tools through `/mcp`. Ghostwriter's
file routes are not tools, so the model cannot call them.

## Callers and doors

| Caller | Door | Files | Tools | Wall cap | Running jobs |
|---|---|---|---|---|---|
| `docint` | mcp | inline | `DOCINT_WORKBENCH_TOOLS` (`run_python`) | `DOCINT_WORKBENCH_WALL_S` (60) | `DOCINT_WORKBENCH_SLOTS` (2) |
| `ghostwriter` | mcp, and seven file routes | workspace | `GHOSTWRITER_WORKBENCH_TOOLS` (the ten) | `GHOSTWRITER_WORKBENCH_WALL_S` (120) | `GHOSTWRITER_WORKBENCH_SLOTS` (3) |

- The door is checked right after the key, before any body is read
  (`workbench/api/auth.py:KeyGate`). docint gets `404` on every path but
  `/mcp`, `/healthz` and `/metrics`. Every such `404` is
  `{"detail": "not found"}`.
- Ghostwriter's key reaches `/mcp` and seven file routes: `/files/upload`
  (`POST` and `DELETE`), `/files/derived`, `/files/prepare`, `/files/view`,
  `/files/list` and `/files/chat`. A purpose it must state and did not is `428`
  on a file route.
- Every path Ghostwriter names is confined to `X-Session-Id`'s chat, anything
  else being `404`; `default` or no session is `400`. For up to an hour after
  `DELETE /files/chat` (`DELETED_CHAT_HOLD_S`), an upload or a tool call for
  that chat is refused (`404` on a route).
- The tool list is enforced on `/mcp`: `tools/call` answers a tool not on the
  caller's list `400`, `-32602`, "unknown tool".
- `X-Accept-Images: 1` on `/mcp` says the call takes images; Ghostwriter 0.3.0
  sends it on every request (`workbench/api/auth.py:accepts_images`). Only then
  is `view_file` listed and callable, and do `read_document` and `view_file`
  send image blocks. A call without it (any other value counts as none), such as
  an older Ghostwriter's, is told and shown what workbench 0.6 told and showed:
  nine tools, `read_document`'s 0.6 description and hints, `write_presentation`
  as 0.8.0 described it (no `view_file`), the 0.6 server instructions, no image
  and no render; a picture file is refused as `unsupported_format`, and
  `view_file` is "unknown tool". The schemas are the same either way. The header is never trusted for access: the key, the tool
  list and the chat decide that, and it only chooses what the model is told and
  shown. The file routes need no such header: only a client that knows
  `/files/prepare` and `/files/view?rendition=` calls them.
- A caller is a namespace: users are `(caller, user-id)`. docint has no folder
  under `workspaces/`.

## The matrix

"The chat" is the chat folder `/workspace/<chat-id>/` of the request
(`X-Session-Id`). "Kept" is what outlives the call. Every new file is a file of
the workspace and is deleted with the others (see [retention.md](retention.md)).
Every tool that runs in the executor also leaves a job record (see
[data-flow.md](data-flow.md#where-it-is-stored)).

### Tools

| Tool | Reads | Writes | Audit `action` / `operation` | Kept |
|---|---|---|---|---|
| `list_files` | names, sizes, dates and kinds of the chat's files, from the database | nothing | not recorded: the user's own metadata | nothing |
| `read_document` | one file, as a copy in a job; a picture is not read in a job but rendered as `view_file` does, and a PDF with pages that have no text layer, or a deck (`.pptx`, `.potx`) with slides that have no text, also has the renders of those pages or slides (a deck converted to a PDF first, in a job), which the api reads back and sends to the model as image blocks, at most 4 images and 2.5 MiB an answer; a clip (audio or video) is read as its transcript, the text file its prepare made from Nextext's JSON Lines, which is the job's one input, at the path its row gives and never one the model names: the clip's own bytes enter no job, and a clip with no transcript is answered at once with no job | for a picture, or a PDF or a deck with such pages, the renders under the reserved `.derived/` folder, the pages or strips not made yet, and a deck's PDF; for a clip, its transcript and frames when it was not prepared yet; no file of the chat | `query` / `tool.read_document`, naming the file and each render shown, before any image leaves; for a clip the clip and its transcript, and no image; the renders made for the call have their `modify` / `file.prepare` event | a job record, and the renders until the source goes |
| `search_files` | the chat's newest 1000 files, as copies in a job, but no clip (a video or audio file the user uploaded: its bytes enter no job, it is counted in `unsupported_files` and the hint says so; a chat of clips alone runs no job); the executor never copies a file below the chat's `out/` folder, though the event names it | nothing | `query` / `tool.search_files` | a job record |
| `view_file` | one picture, PDF or Office file (`.docx`, `.dotx`, `.pptx`, `.potx`, `.odt`) of the chat, as a copy in a job when a page is not rendered yet (an Office file is converted to a PDF first, in the same turn); its renders, which the api reads back and sends to the model as image blocks, at most 8 images and 2.5 MiB an answer; a clip (audio or video) is never read: its frames are the rows its prepare made from Nextext's keyframes zip, read back and sent as image blocks, never decoded in the api, by number, range or time (`frame 3`, `frames 2-5`, `at 01:24`), and a frame whose row or file is gone is named in the hint, not shown (a frame is not made on its own: it comes back only with the whole set, when the clip's transcript, else its frame 1, is gone and the next prepare runs); any other file is answered at once, with no image and no job | the renders of the file under the reserved `.derived/` folder, the pages or strips not made yet, and an Office file's PDF; for a clip nothing (its frames exist already, and `strips` is ignored); no file of the chat | `query` / `tool.view_file`, naming the file and each render or frame shown, before any image leaves; the renders made for the call have their `modify` / `file.prepare` event | the renders, until the source goes, and a job record when a job ran |
| `run_python` | every file of the chat, the newest 1000 outside `out/`, as read-only copies in a job, but no clip (a video or audio file the user uploaded: its bytes enter no job, so the sandbox cannot decode it, and the hint says that video and audio are not in it: a clip's transcript is read with `read_document`, its frames looked at with `view_file`, which a call without `X-Accept-Images: 1` is not told of) | the job's `out/`: at most 20 files, which become new files of the chat; and its private `home/` and `tmp/`, removed with the job | `combine` / `tool.run_python` | the new files and a job record |
| `inspect_template` | one template or slide master, as a copy in a job; no file without a path (the plain slide master) | nothing | `query` / `tool.inspect_template` | a job record |
| `fill_template` | the template and every file a value's source names, as copies in a job | one new `.docx` or `.xlsx` in the template's chat; the template is not changed | `modify` / `tool.fill_template` | the new file and a job record |
| `write_table` | nothing of the chat: the values come in the call | one new `.xlsx` or `.csv` in the chat | `collect` / `tool.write_table` | the new file and a job record |
| `write_document` | nothing of the chat: the Markdown comes in the call | one new `.docx` or `.md` in the chat | `collect` / `tool.write_document` | the new file and a job record |
| `write_presentation` | the slide master (`.pptx` or `.potx`), if one is given, every picture of the chat a slide shows (PNG, JPEG or GIF) and every file a slide's source names, as copies in a job | one new `.pptx` in the master's chat, or without a master in the request's chat (a `.potx` master gives a `.pptx`); the master, the pictures and the sources are not changed | `modify` / `tool.write_presentation` | the new file and a job record |
| `run_python` (inline, docint) | the data of the call, as read-only files of a job | the job's `out/`, `home/` and `tmp/`, removed with the job; the outputs of the allowed types come back in the answer | `combine` / `tool.run_python.inline` | a job record; no file |

The ten tools answer Ghostwriter with `outputs` besides their own answer: the
id of each file the call wrote and, for a csv or xlsx file, a preview of it.

Sources in the code: `workbench/api/tools.py` (`list_files`,
`search_files`, `run_python`),
`workbench/api/tools_templates.py` (`inspect_template`, `fill_template`),
`workbench/api/tools_output.py` (`write_table`, `write_document`),
`workbench/api/tools_presentations.py` (`write_presentation`),
`workbench/api/tools_views.py` (`read_document`, `view_file`).

### File routes

Ghostwriter's routes (`workbench/api/files.py`), each held to the request's
chat.

| Route | Reads | Writes | Audit `action` / `operation` | Kept |
|---|---|---|---|---|
| upload, `POST /files/upload` | the uploaded bytes | one new file in the chat folder; a taken name gets a number | `collect` / `file.upload`, before the file is visible | the file |
| artifact of a clip, `POST /files/derived?source=<path>&role=nextext_jsonl\|nextext_zip` | the request body, which is the artifact (Nextext's JSON Lines or its keyframes zip, at most 8 MiB or 32 MiB), read whole into a `.part`; the api reads no more of it than the first four bytes of a zip (`PK\x03\x04`) or the first line of a jsonl (at most 64 KiB, a JSON object), and the executor parses everything else, later | one file under the reserved `.derived/` folder of the clip, which no path reaches, beside the clip's other derived files; a clip of the request's chat that is an audio or video upload (any other source is `422 unsupported_format`, another chat's or a `.derived` path `404`); it replaces the clip's artifact of that role and the transcript and frames made from the old one, and a refusal remembered for the clip is forgotten; it counts in the user's quota and the clip's 64 MiB of derived files (`507`); it waits for the clip's lock up to 120 s (`503`) | `modify` / `file.derived`, before the artifact is visible; naming the clip, the new file and each file it replaces | the artifact, until the clip goes (or a second one of its role replaces it); the answer is its `id`, `role`, `bytes` and the `id` it `replaced` |
| prepare, `POST /files/prepare` | a picture, PDF or Office file (`.docx`, `.dotx`, `.pptx`, `.potx`, `.odt`), as a copy in a job of the executor; an Office file is converted once to a PDF, kept as a derived file of it, and then drawn from that PDF, both jobs in one turn of the caller's conversion gate (a picture or a PDF waits for its prepare gate, apart from it); a file the executor refused for what it is is refused again, with no job; any other file is answered as `kind: none` without a job | the renders of the file, under the reserved `.derived/` folder of the chat, which no path reaches: the first 20 pages of a PDF or Office file, or the picture, and an Office file's PDF beside them; with `strips` 2 or 3 (default 1; any other is `422`), also the picture's strips, in a second job on the same prepare gate, while the pages of a PDF or Office file stay to be cut when viewed | `modify` / `file.prepare`, before any render or PDF is visible, one for each job | the renders and the PDF, until the source goes; the answer is their manifest, the same whatever `strips` is |
| view and download, `GET /files/view` | one file's bytes, sent to the caller; with `rendition`, one render of it (a page, a slide or a picture, or one strip of it), made first when it does not exist yet, on the user's own turn (an Office file with no PDF kept is converted first and drawn, in one turn of the caller's conversion gate); of a clip, its frame `rendition`, whole, as its prepare kept it (`strips` above 1 is `422`, with or without a `strip`; a number past the last frame, or a frame whose row or file is lost, is `404`, never `500`) | nothing, or the renders of that page, as a prepare keeps them | `disclose` / `file.view`, naming the file, and the render with `rendition`; a render made first has its `modify` / `file.prepare` event too | nothing, or the render |
| `DELETE /files/upload` (since 0.8.1; 0.8.0 answers `405`) | nothing | one upload of the chat, the user's, that Ghostwriter takes back (it does so only for one not yet sent; workbench deletes any upload of the chat), with its renders and their rows; a tool's output, another chat's file or a render is not found (`404`); a job still running on it keeps no render; a refusal remembered for it is forgotten | `delete` / `file.delete`, first | nothing |
| `DELETE /files/chat` | nothing | the chat's files and renders and their rows, its job rows and its folder | `delete` / `chat.delete`, first | nothing |
| list, `GET /files/list` | names, types, sizes and dates of one folder | nothing | not recorded | nothing |

The service routes (`/healthz`, `/metrics`) read no file content and are not
recorded (`workbench/api/health.py`, `workbench/api/metrics.py`). Every other
path answers `404` to a caller with the key, and `401` to anyone without it.

[audit.md](audit.md) says what each event holds.

## The boundaries

### The user is the boundary; the chat is a folder

- A caller is known by its bearer key, compared in constant time
  (`workbench/api/auth.py:CallerRegistry.authenticate`). The key is checked
  before anything of a request's body is read, whatever its path: without it a
  request answers `401`, never `404`, `405` or `422` (`auth.py:KeyGate`), and
  `/healthz` and `/metrics` are the only routes that need none. The user is the
  caller's `X-User-Id`, trusted because only a caller with the key reaches the
  api (`workbench/api/auth.py:require_principal`). Every route that touches
  files needs it.
- The user's files live under `workspaces/<caller>/<user-id>/` on the data
  volume. Every path a request names is resolved below that root and nowhere
  else (`workbench/paths.py:Workspaces.resolve`). A path that leaves
  `/workspace` once normalised, a NUL byte, an invalid name and a symlink in
  the way are refused; workbench never makes a symlink.
- Another user's file answers `404`, the same answer as a file that does not
  exist (`workbench/api/errors.py`).
- The chat is a folder under the user's root. `X-Session-Id` names it, and
  Ghostwriter is held to it: a path in another chat answers `404`, as a file
  that does not exist (`workbench/api/store.py:Store._locate`). So:
  - `list_files`, `search_files` and `run_python` see only the request's chat;
    the last two are given none of its clips, and `inspect_template`,
    `fill_template` and `write_presentation` refuse one named as a template, a
    master or a file source, before any job, as `422 unsupported_format: a clip
    is read with read_document`, a word that names no `view_file` to a call
    without images: a clip's bytes enter no job, its
    own prepare's reading Nextext's artifacts of it, never the clip;
  - `fill_template` and `write_presentation` take file sources only from the
    template's or master's chat (without a master, the request's), and
    `write_presentation` its pictures too.
- Files exist only inside chat folders. A file directly under `/workspace`
  cannot be made there, and `/workspace` and a chat folder are never the
  target of a file operation (`workbench/api/store.py:Store._file_target`).
- Uploads are never overwritten by a tool. Every tool result is a new file, and
  a taken name gets a number (`workbench/api/store.py:Store.register_output`).

### No network

The executor, where every file is opened, has no network at all
(`network_mode: none` in `docker/compose.yaml`), and a job has no socket at all.
A job sees only copies of its own chat's files, read-only (a docint job, only
the data its call brings), and can write only into its own `out/` folder and its
private `home/` (with its `tmp/`); both are removed with the job, and only what
is in `out/` can become a file of the workspace (see [sandbox.md](sandbox.md)).
The api calls no model and joins only `workbench-net`.

### The agent deletes nothing

No tool deletes or changes a file. Files go only when Ghostwriter deletes a
chat for its user (`DELETE /files/chat`) or takes back an upload of the chat
(`DELETE /files/upload`, never a tool's output; Ghostwriter does so only for one
its user has not sent), or by retention after
the period (`workbench/api/retention.py:Sweeper`).

## Which runs are audited

- Every tool job whose answer reached the api is recorded before anything of
  that answer goes out: `ok` for an answer the api sends on, `error` for any
  other (`workbench/api/tools.py:record_answer`, `OutputKeeper`).
- A refusal is recorded too, as `error` with `refused: <code>`. Its message
  goes to the model and can quote the file: a workbook's sheet names, a line of
  a template, a drop-down's entries, the sheets and range of a value's source,
  the layouts of a slide master. The event holds the code alone, and if it
  cannot be written the model gets `audit unavailable`, never the refusal.
- `run_python` names the files the job was given and the files it made; a fill
  or a write names its new file. Each is recorded before any file it made
  becomes visible. A new file the model is not told of (the answer could not be
  used) is never moved into the chat.
- A job whose answer never reached the api (the connection dropped, or the
  request was cancelled while the job ran) is not recorded as a tool call.
  Nothing of it reached the model, and the files it made are never kept
  (`workbench/api/jobs.py:JobQueue`). Its job record stays, until retention.
- What a call makes and removes within the same request is covered by that
  call's event: the job's folder in the executor. A new file named in the event
  that could not be stored after it (the user's quota, a full disk, an invalid
  name, any other error of the store), or that was stored and then had to go
  again because the call failed, whether it went or not, is recorded again with
  `outcome: error`. Only what outlives its request otherwise gets an event of
  its own, from retention.
- A new file whose event cannot be written is never stored, and the call fails
  (see [audit.md](audit.md#fail-closed)).

## What the model is told

The tool descriptions and the hints in a result tell the model, among other
things, the limits that matter to users:

- Only this chat's files are available to `run_python`. Code that looks for a
  file of another chat gets a hint saying so
  (`workbench/executor/runners/python.py:run_python`).
- Use threads, not multiprocessing. `multiprocessing` and process pools do not
  work in a job (see [sandbox.md](sandbox.md#limits-of-run_python)).
- For people who open a table in Excel, `write_table` writes `xlsx`. Excel reads
  a CSV file by the computer's regional settings: German Excel splits it on `;`
  and may read `1.5` as text or a date. CSV, comma-separated unless the call
  asks for `;` or a tab, is for other programs.
- A data file (JSON, CSV, XLSX) becomes a table file through `run_python`,
  which loads it in code and writes `out/` as `.xlsx` or `.csv`; its rows are
  not copied into `write_table`. `read_document` shows a JSON file indented,
  one value per line, and JSON Lines (`.jsonl`, `.ndjson`) line by line.
- Pages, slides and pictures come back as images for the model alone, on a
  call that takes them (`X-Accept-Images: 1`, see [Callers and
  doors](#callers-and-doors)), after the answer's text, and `shown` names each
  in order (image block `i` is `shown[i]`). `read_document` lists the pages of
  a PDF that have no text layer, and the slides of a deck (`.pptx`, `.potx`)
  that have no text, in `pages_without_text`, never as blank, and attaches
  their images in the same answer: the pages or slides of its window, whole,
  at most 4 images and 2.5 MiB, and the answer ends before the first that
  does not fit, with `next_part` naming it (`page 5`, `slide 5`). A deck whose
  drawn pages are not as many as its slides attaches none, since a picture's
  number may not be its slide's, and the hint says that `view_file` shows the
  slides as drawn. A Word or OpenDocument text file (`.docx`, `.dotx`, `.odt`)
  is read as text alone, and its first read says that `view_file` shows its
  pages. A picture file is read as its image. A clip (video or audio) is
  read as its transcript, whether or not the call takes images, since the
  transcript is text: `kind` is `transcript`, `parts` are lines as a text
  file's are (`lines 1-200`), `total_parts` the lines, and `shown` is empty. A
  line reads `[01:24] Speaker 1: text`, `[1:02:03] text` from one hour on, and
  has no speaker label when the sentence had none. A call that takes images
  is told after the hint that `view_file` shows the clip's frames, when it
  has any; a call that takes none is told of no frame. A clip with no speech
  is answered with no part and the hint "No speech was found in this clip.",
  one whose JSON Lines of speech gave no transcript with "The transcript could
  not be read.", one nothing was found of with the manifest's own hint.
  `view_file` shows any page, slide or picture, drawn as it would print, at most 8 images; a deck's are
  labelled and paged by slide (`slide 3`, `slides 9-12`), and `unit` says
  which. With no part it shows the first four pages or slides, and its hint
  names those after them and the part that shows the next four. Spreadsheets
  are not shown, and the hint says to read them with `read_document`. A
  picture of several frames is shown as its first, and the hint says so; a
  deck whose drawn pages are fewer than its slides is told so; a page that
  could not be shown is named in a sentence. A clip's frames are labelled
  `frame 3 (01:24)` and asked for as `frame 3`, `frames 2-5` or `at 01:24`
  (the frame nearest that time); with no part it shows up to 8 frames spread
  over the clip, and the hint says so. Audio has no frames: the hint says to
  read its transcript with `read_document`. A read whose images cannot be
  made (the executor has no room, the conversion or the render is refused,
  the user's quota or the file's cap of renders is spent) still answers the
  text, with `shown` empty and a hint: for a cause that may pass (no room now,
  a job or its reading that ran out of time) that `view_file` shows the pages
  or slides, and for one that will not (the file's cap of renders, the quota,
  a full volume, a file that cannot be drawn) the cause, with no promise of
  `view_file`; a page that cannot be read back is named in the hint. When the
  images end the answer early, the hint says so and `pages_without_text` lists
  only the pages before `next_part`. A picture file has no text to fall back
  on, so its read fails with the render. None of this is told or shown to a
  call without `X-Accept-Images: 1`: it reads every Office file as text, as
  0.6 did.
- A Word table holds at most 63 columns. `write_document` refuses a wider
  Markdown table (`refused`), with a text that points to `write_table`.
- A deck holds 1 to 30 slides and at most 6 blocks a slide, one for each place
  of its layout. A text block holds at most 2000 characters, a title 300, a
  subtitle 500 and the speaker notes 5000. A table holds at most 30 rows (the
  header counts), 10 columns and 500 characters a cell; a chart at most 50
  categories and 10 series, a value or null for each category, and a pie one
  series of values that are not negative. A deck shows at most 20 pictures (PNG,
  JPEG or GIF files of the chat, each at most 10 MiB and 40 megapixels, 30 MiB
  together) and a slide cites at most 20 sources. The api checks every bound
  before it sends a job, so a deck over one is a `422` that names the slide and
  the block, with no job and no event, and the runner checks them again
  (`workbench/decks.py`). Text that may not fit its place is shrunk to 60 % at
  most, and a warning names the slide when that does not do (the answer's
  `warnings`).
- A deck is read as the model meant it, as models write one by mistake. A
  block wrapped as `{"role": …, "text": …}`, content given as one block or as a
  list in a string, a block written as JSON in a string or as `{image: …}`, and
  line breaks written out before a list's next item are read as the block meant
  (`workbench/api/tools_presentations.py:_read_as_meant`); every check then runs
  on that block. A slide that names no layout takes the first layout that has
  its title, its subtitle and one place a block that takes it, one whose title
  and subtitle are exactly the slide's first (so a title alone gets a title-only
  layout); and a title slide's only text block, within a subtitle's 500
  characters, is its subtitle (`workbench/executor/presentations/deck.py`).
- A slide master's example slides never reach the new deck. They are removed,
  with the custom shows and sections that name them and the thumbnail and
  `docProps/app.xml` that show or list them; the deck takes the master's theme,
  layouts and media, none of the parts that name who edited or commented on the
  master, and of its core properties only the language. A master whose layouts
  or master slide link to its own slides is refused (`invalid_template`), a
  macro-enabled presentation or a slide show too (`unsupported_template`), and
  `fill_template` refuses a slide master and names `write_presentation`.
- Without a master, a deck is built in the plain master
  (`workbench/executor/presentations/master.py:plain_master`): python-pptx's
  own default presentation in the executor image, with the Office theme, its
  two layouts of vertical text left out and widened to 16:9. No file of the
  chat is read for it, and `inspect_template` without a path lists its nine
  layouts.
- Other limits: at most 20 new files per `run_python` call; a job is given the
  newest 1000 files of the chat; answers are cut at `WORKBENCH_RESULT_MAX_KB`
  and say how to go on; Outlook `.msg` files cannot be read.
