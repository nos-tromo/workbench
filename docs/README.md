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
under its own licence; four need a note, and a fifth that no image ships. The
executor image also ships LibreOffice and its fonts, all Debian packages, noted
below:

- **docxtpl** is LGPL-2.1. workbench uses it unmodified, as a separate Python
  package, and its source is in the executor image as installed, so it can be
  replaced there.
- **odfpy** is offered under several licences; workbench uses it under its
  Apache-2.0 option.
- **pi-heif**'s wheel bundles libheif and libde265, both LGPL-3.0. workbench
  uses them unmodified, as the shared libraries the wheel ships, so they are
  replaceable in the executor image. They decode HEIC pictures there and
  nowhere else.
- **pypdfium2** is Apache-2.0 or BSD-3-Clause, and its wheel carries pdfium and
  the licences of pdfium's own bundled libraries, which ship with it.
- **pillow-heif** (the writer the tests make a HEIC picture with) bundles the
  GPL-2.0 x265 encoder. It is in the `dev` dependency group and the `test` stage
  of the executor's Dockerfile only, never in an image that is shipped.
- **LibreOffice** converts Word, PowerPoint and OpenDocument text files to PDF
  in the executor, through LibreOfficeKit. It comes from Debian's
  `libreoffice-core-nogui`, `-writer-nogui`, `-impress-nogui` and `-calc-nogui`
  packages and the packages they pull in (25.2.3 at 0.8.0). workbench uses it
  unmodified, as Debian packages, so it is replaceable in the executor image: it
  is one `apt-get install` layer of `docker/Dockerfile.executor`, and its source
  is Debian's. The `/usr/share/doc/<package>/copyright` file of each
  `libreoffice-*` package is the same file. It says the suite is MPL-2.0, with
  some files that incorporate Apache-2.0 material, and names other licences for
  parts of the source tree and for what Debian adds: Apache-2.0, MIT and Expat,
  BSD-2-clause and BSD-3-clause, MPL-1.1, LGPL-2, LGPL-3+, GPL-2, GPL-2+ and
  GPL-3+, CC0-1.0 and CC-BY-SA-3.0. The image carries no Java runtime and no
  Python script provider.
- **The fonts** LibreOffice draws with. The Dockerfile installs five font
  packages, `fonts-crosextra-carlito`, `fonts-crosextra-caladea`,
  `fonts-liberation2`, `fonts-dejavu-core` and `fonts-noto-core`, and with what
  they and LibreOffice pull in, eight packages carry fonts in the executor
  image. All are Debian's and unmodified:
  - Carlito (`fonts-crosextra-carlito`) and Caladea (`fonts-crosextra-caladea`)
    stand in for Calibri and Cambria;
  - Liberation is held by `fonts-liberation`; `fonts-liberation2`, which the
    Dockerfile names, is a transitional package with no font in it that depends
    on `fonts-liberation`;
  - DejaVu is in `fonts-dejavu-core` and `fonts-dejavu-mono`, which it pulls in;
  - Noto is in `fonts-noto-core` and `fonts-noto-mono`, which it pulls in;
  - OpenSymbol is `fonts-opensymbol`, which `libreoffice-core-nogui` pulls in.

  Their copyright files say Carlito, Caladea, Noto (core and mono) and
  Liberation are under the SIL Open Font License 1.1 (`OFL-1.1`; the Liberation
  file writes `SIL-OFL-1.1`), DejaVu (core and mono) under the Bitstream Vera
  licence, with DejaVu's own changes in the public domain, and OpenSymbol under
  MPL-2.0, in the LibreOffice copyright file (some of its files incorporate
  Apache-2.0 material) that `fonts-opensymbol` carries as well. They name the
  Debian packaging of Carlito, Liberation and DejaVu as GPL-2+, of Noto as
  GPL-3+ and of Caladea as Apache-2.0. A font only draws the pages: the
  converted PDF, which embeds subsets of the fonts, never leaves workbench, and a
  render is a picture.

No Python package the images could use only under the GPL or the AGPL is
installed: of the shipped packages, only docxtpl, odfpy and pi-heif (through the
libraries its wheel bundles) name a GNU licence, the first and the last the LGPL
and odfpy one option among several. That is why Outlook `.msg` files are not
read: the Python parser for them, `extract-msg`, is GPL. `read_document` asks
the user to save such a file as `.eml` or PDF.

The images' Debian base, `python:3.12-slim-trixie`, carries the operating
system's own packages under their own licences, GPL ones among them; the
executor image adds LibreOffice and the fonts above to it.
