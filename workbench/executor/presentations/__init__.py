"""Presentations in a user's slide master, read and built inside the sandboxed job.

A ``.pptx`` or ``.potx`` is a slide master: :mod:`.master` reads its layouts, each layout's places in reading order and
about how much text fits in each, which ``inspect_template`` answers and a deck is built on. A refusal is the
templates' :class:`~workbench.executor.templates.Refusal`, so the api maps it as it maps theirs.
"""
