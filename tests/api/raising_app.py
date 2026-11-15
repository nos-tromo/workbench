"""Test-only: the real api over a store whose reads fail with an error that names a path; never deployed.

``tests/api/test_internal_errors.py`` runs it under uvicorn (``uvicorn --factory tests.api.raising_app:factory``), as
the api image does, to see what the server logs. The store's ``open_file`` and ``files_in_chat`` raise
``PermissionError`` for a synthetic file of a synthetic user, as a permission problem on the volume would.
"""

import os
from typing import BinaryIO, Final, NoReturn, override

from fastapi import FastAPI

from workbench import __version__
from workbench.api.audit import SERVICE, AuditLog
from workbench.api.main import create_app
from workbench.api.store import Store
from workbench.config import load_settings
from workbench.layout import DATABASE
from workbench.models import FileRecord, Principal
from workbench.paths import Workspaces
from workbench.utils.audit_event import AuditEmitter

#: What the error names: a synthetic path of a synthetic user's file.
PATH: Final = "/data/workspaces/user-a/chat-1/quarterly-figures.xlsx"


def _refuse() -> NoReturn:
    """Fail as a file the api may not read does, its path in the message."""
    raise PermissionError(13, "Permission denied", PATH)


class RefusingStore(Store):
    """A store whose reads of files fail with :func:`_refuse`."""

    @override
    def open_file(self, p: Principal, path: str) -> tuple[FileRecord, BinaryIO]:
        """Fail."""
        _refuse()

    @override
    def files_in_chat(self, p: Principal) -> list[FileRecord]:
        """Fail."""
        _refuse()


def factory() -> FastAPI:
    """Build the api from the environment over a :class:`RefusingStore`, its audit chain on stdout.

    Returns:
        The application.
    """
    settings = load_settings(os.environ, role="api")
    store = RefusingStore(settings.data_dir / DATABASE, Workspaces(settings.data_dir))
    return create_app(settings, store=store, audit=AuditLog(AuditEmitter(SERVICE, __version__)))
