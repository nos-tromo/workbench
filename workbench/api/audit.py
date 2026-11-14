"""The api's data-protection audit chain.

Every file and tool action is recorded by reference: file ids, counts and the
chat folder the files are in as container. Never content and never a file name, so nothing here takes
one. One emitter, and so one hash chain, per process; the lifespan opens it with
:meth:`AuditLog.open` and ends it with :meth:`AuditLog.close`.

The acting user is the caller's own id for the end user (``via="service"``), and
the authenticated caller is always in the event's criteria. ``parent_event_id``
is not set: Open WebUI's tool calls carry no message id, so the events of one
agent turn are tied together only by their ``container`` and their order in the
chain.

A failed write raises :class:`~workbench.utils.audit_event.AuditWriteError`; the
caller turns it into a refusal and returns nothing, because an action that cannot
be recorded must not happen.
"""

from collections.abc import Mapping, Sequence
from typing import Final, Self

from workbench.config import Settings
from workbench.models import Principal
from workbench.utils.audit_event import Actor, AuditEmitter, Purpose, Target
from workbench.utils.audit_runtime import open_audit_stream

#: The compose service name; the audit store binds each chain to it.
SERVICE: Final = "workbench"

#: Each event key and the ``(action, operation)`` it is recorded under.
OPERATIONS: Final[dict[str, tuple[str, str]]] = {
    "upload": ("collect", "file.upload"),
    "view": ("disclose", "file.view"),
    "read": ("disclose", "file.read"),
    "display": ("disclose", "file.display"),
    "delete": ("delete", "file.delete"),
    "read_document": ("query", "tool.read_document"),
    "search_files": ("query", "tool.search_files"),
    "run_python": ("combine", "tool.run_python"),
    "inspect_template": ("query", "tool.inspect_template"),
    "fill_template": ("modify", "tool.fill_template"),
    "write_table": ("collect", "tool.write_table"),
    "write_document": ("collect", "tool.write_document"),
}

_SYSTEM_ACTOR: Final = Actor(user=None, via="system")
_FILE: Final = "file"


class AuditLog:
    """The api's one audit chain, with one method per kind of event.

    Attributes:
        emitter: The process's emitter.
    """

    def __init__(self, emitter: AuditEmitter) -> None:
        """Wrap ``emitter``; nothing is written until :meth:`start`.

        Args:
            emitter: The process's emitter, not yet started.
        """
        self.emitter = emitter

    @classmethod
    def open(cls, settings: Settings, version: str) -> Self:
        """Create the process's log, writing where ``AUDIT_OUTPUT`` selects.

        Call it once per process, from the FastAPI lifespan. The stream stays
        open for the life of the process.

        Args:
            settings: The api's settings; ``audit_output`` selects the stream.
            version: The running release, for every event.

        Returns:
            The log, not yet started.

        Raises:
            ValueError: If ``AUDIT_OUTPUT`` names an unknown mode.
            OSError: If the container stream cannot be opened.
        """
        return cls(AuditEmitter(SERVICE, version, stream=open_audit_stream(settings.audit_output)))

    def start(self, config: Mapping[str, object]) -> None:
        """Open the chain and record the operating configuration.

        Writes the chain's ``start`` event, then an ``admin`` / ``config.audit``
        event whose criteria are ``config``. Pass limits and periods only,
        never a key or any other secret.

        Args:
            config: The limits and periods in force.

        Raises:
            AuditWriteError: If an event cannot be written; startup must fail then.
        """
        self.emitter.start()
        self.emitter.emit("admin", "config.audit", _SYSTEM_ACTOR, criteria=config)

    def record(
        self,
        key: str,
        p: Principal,
        *,
        file_ids: Sequence[str] = (),
        outcome: str = "ok",
        criteria: Mapping[str, object] | None = None,
        purpose: Purpose | None = None,
    ) -> str:
        """Record one file or tool action of a user, by file id.

        Args:
            key: The action, a key of :data:`OPERATIONS`.
            p: The caller, the user and the chat folder of the files the event names; for an
                action on a file that is the folder the file lives in, never the chat of the request.
            file_ids: Ids of the files the action touched; never names.
            outcome: ``ok``, ``denied`` or ``error``.
            criteria: The action's parameters, never content. ``caller`` is
                added, and replaces a ``caller`` given here.
            purpose: The stated purpose, if any.

        Returns:
            The id of the written event.

        Raises:
            KeyError: If ``key`` is not in :data:`OPERATIONS`.
            AuditWriteError: If the event cannot be written.
            RuntimeError: If the chain is not started or already closed.
        """
        action, operation = OPERATIONS[key]
        # caller goes first so it is among the criteria the emitter keeps when it caps their number.
        merged: dict[str, object] = {"caller": p.caller}
        merged.update((name, value) for name, value in (criteria or {}).items() if name != "caller")
        event = self.emitter.emit(
            action,
            operation,
            Actor(user=p.user_id, via="service"),
            outcome=outcome,
            purpose=purpose,
            target=Target(kind=_FILE, ids=file_ids, count=len(file_ids), container=f"chat:{p.chat_id}"),
            criteria=merged,
        )
        return str(event["event_id"])

    def record_system(
        self,
        action: str,
        operation: str,
        *,
        file_ids: Sequence[str] = (),
        criteria: Mapping[str, object] | None = None,
        outcome: str = "ok",
        count: int | None = None,
        container: str | None = None,
    ) -> str:
        """Record an action of the service itself, such as a retention sweep.

        The event has a target when it names a file or gives a count or a container; otherwise it has none.

        Args:
            action: One of the audit actions.
            operation: What exactly happened, dotted, e.g. ``retention.sweep``.
            file_ids: Ids of the files it touched; never names.
            criteria: The rule or parameters, never content.
            outcome: ``ok``, ``denied`` or ``error``.
            count: How many files it touched, when not every one has an id to name; ``len(file_ids)`` by default.
            container: The chat folder of the files, as ``chat:<chat-id>``.

        Returns:
            The id of the written event.

        Raises:
            ValueError: If ``action`` or ``outcome`` is unknown.
            AuditWriteError: If the event cannot be written.
            RuntimeError: If the chain is not started or already closed.
        """
        target = None
        if file_ids or count is not None or container is not None:
            target = Target(
                kind=_FILE, ids=file_ids, count=len(file_ids) if count is None else count, container=container
            )
        event = self.emitter.emit(action, operation, _SYSTEM_ACTOR, outcome=outcome, target=target, criteria=criteria)
        return str(event["event_id"])

    def close(self) -> None:
        """End the chain with its ``shutdown`` event; later calls do nothing.

        Raises:
            AuditWriteError: If the shutdown event could not be written.
        """
        self.emitter.close()
