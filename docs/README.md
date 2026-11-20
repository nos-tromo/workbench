# workbench documentation

The reference for workbench, the federation's shared tool tier. The top-level
[`README.md`](../README.md) says what it is and how to start it; `CLAUDE.md` is
the design record for contributors and coding agents.

| Document | What it covers |
|---|---|
| [configuration.md](configuration.md) | Every variable of `.env.example` with its default and meaning, the formulas for the executor's limits, the caller keys shared with docint and Ghostwriter, what is not exposed and why |
| [permissions.md](permissions.md) | Per tool and per file route: what it reads, writes, records and keeps; the boundaries (the user, the chat as a folder, no network, no deletion by the agent); which runs are audited; what the model is told |
| [data-flow.md](data-flow.md) | What enters, where it is stored, for how long, and where it goes, the callers' chat history included; the facts for the record of processing and the impact assessment |
| [audit.md](audit.md) | The data-protection audit log: every event, its target and criteria, what is never recorded, the retention events, the chain, fail closed |
| [retention.md](retention.md) | Every file deleted a fixed period after it was made, each deletion recorded first; leftovers, folders, job records, the checkpoint; a changed period; the dry run |
| [sandbox.md](sandbox.md) | The executor: its capabilities and why, the uid pool and slot lifecycle, the limits, the start-up checks, output validation, the residual risks, gVisor, what a job can import, the limits of `run_python` |

Design history: the design is
[`superpowers/specs/2026-11-13-workbench-design.md`](superpowers/specs/2026-11-13-workbench-design.md),
the build plans are in [`superpowers/plans/`](superpowers/plans/). Where they
and these documents differ, these documents describe the code.

## Who this is for

- **Operators** deploying workbench: [configuration.md](configuration.md),
  then [retention.md](retention.md) for the deletion that is always on.
- **Whoever assesses the processing** (data protection, security):
  [data-flow.md](data-flow.md), [permissions.md](permissions.md),
  [audit.md](audit.md) and [sandbox.md](sandbox.md).
- **Developers**: `CLAUDE.md`, then the documents above for each part.

## Third-party licences

workbench is Apache-2.0. Its images ship third-party Python packages, each
under its own licence; two need a note:

- **docxtpl** is LGPL-2.1. workbench uses it unmodified, as a separate Python
  package, and its source is in the executor image as installed, so it can be
  replaced there.
- **odfpy** is offered under several licences; workbench uses it under its
  Apache-2.0 option.

No Python package the images could use only under the GPL or the AGPL is
installed: of the shipped packages, only these two name a GNU licence. That is
why Outlook `.msg` files are not read: the Python parser for them,
`extract-msg`, is GPL. `read_document` asks the user to save such a file as
`.eml` or PDF.

The images' Debian base, `python:3.12-slim-trixie`, carries the operating
system's own packages under their own licences, GPL ones among them.
