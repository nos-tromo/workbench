"""Pictures a model can look at, made inside the sandboxed job.

:mod:`~workbench.executor.render.images` decodes a source picture, turns it upright and brings it to the bounds of
:mod:`workbench.renders`; :mod:`~workbench.executor.render.encode` writes the result as a JPEG or PNG under the size
cap, with none of the metadata the source carried, and cuts it into strips; :mod:`~workbench.executor.render.pages`
opens a PDF, finds its pages with no text and draws one page at the size of a render. All of them run only in the
executor, whose image holds Pillow, pi-heif and pdfium and which has no network: the api never imports this package or
those libraries. The runner that puts them together is :mod:`workbench.executor.runners.render`.

The files are untrusted. A file that cannot be read is :class:`~workbench.executor.readers.UnreadableFile`, with a short
fixed message that names no path.
"""
