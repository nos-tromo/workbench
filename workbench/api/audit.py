"""The api's data-protection audit chain.

Every file and tool action is recorded by reference: file ids, counts and the
chat folder the files are in as container; data that came with a call is counted,
never named. Never content and never a file name, so nothing here takes
one. One emitter, and so one hash chain, per process; the lifespan opens it with
:meth:`AuditLog.open` and ends it with :meth:`AuditLog.close`.

The acting user is the caller's own id for the end user (``via="service"``), and
the authenticated caller is always in the event's criteria. An action's event has
no ``parent_event_id``: Open WebUI's tool calls carry no message id, so the events
of one agent turn are tied together only by their ``container`` and their order in
the chain. Only a continuation, below, names a parent.

An event names at most 200 file ids (``MAX_IDS``). An action on more files is
split, never cut: its event carries the first 200 ids and the full count, and each
further 200 go on a continuation event, ``<operation>.ids``, whose
``parent_event_id`` is that event's id (``audit_runtime.id_chunks``, ADR 0003).

A failed write raises :class:`~workbench.utils.audit_event.AuditWriteError`, a
continuation's as much as the event's; the caller turns it into a refusal and
returns nothing, because an action that cannot be recorded must not happen.

A call that states its purpose and request id in its headers records through a log
bound to them (:meth:`AuditLog.bound`), on the same chain. Such a log also refuses an
action that must state a purpose and did not (:meth:`AuditLog.check`), after recording
the refusal.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Self

from workbench.config import Settings
from workbench.models import Principal
from workbench.utils.audit_event import Actor, AuditEmitter, Purpose, Target
from workbench.utils.audit_runtime import id_chunks, open_audit_stream

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
    "run_python_inline": ("combine", "tool.run_python.inline"),
    "inspect_template": ("query", "tool.inspect_template"),
    "fill_template": ("modify", "tool.fill_template"),
    "write_table": ("collect", "tool.write_table"),
    "write_document": ("collect", "tool.write_document"),
}

_SYSTEM_ACTOR: Final = Actor(user=None, via="system")
_FILE: Final = "file"

#: What the operation of a continuation event ends with.
CONTINUATION: Final = ".ids"


class PurposeRequired(Exception):
    """A purpose the call had to state was not stated; the ``denied`` event is already written."""


def stated(purpose: Purpose | None) -> bool:
    """Say whether a call stated a purpose.

    Args:
        purpose: The purpose the call stated, or ``None``.

    Returns:
        Whether it holds a case reference or a reason.
    """
    return purpose is not None and bool(purpose.to_dict())


@dataclass(frozen=True)
class _Binding:
    """What one call's headers say, for the log bound to it.

    Attributes:
        purpose: The purpose the call stated, or ``None``.
        request_id: The call's request id, or ``None``.
        required: The audit actions the call must state a purpose for.
    """

    purpose: Purpose | None
    request_id: str | None
    required: frozenset[str]


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
        self._binding: _Binding | None = None

    def bound(
        self, purpose: Purpose | None, request_id: str | None, *, required: frozenset[str] = frozenset()
    ) -> "AuditLog":
        """Make a log for one call, on the same chain: its purpose, its request id and the actions that need a purpose.

        Args:
            purpose: The purpose the call stated, or ``None``.
            request_id: The call's request id, or ``None``.
            required: The audit actions the call must state a purpose for (:meth:`check`).

        Returns:
            A log on this log's emitter whose :meth:`record` writes ``purpose`` and ``request_id`` where a call gives
            none. This log is unchanged.
        """
        log = AuditLog(self.emitter)
        log._binding = _Binding(purpose, request_id, required)
        return log

    def check(self, key: str, p: Principal) -> None:
        """Refuse an action the call must state a purpose for and did not, after recording the refusal.

        A log that is not :meth:`bound` checks nothing.

        Args:
            key: The action, a key of :data:`OPERATIONS`.
            p: The caller, user and chat of the request; the chat is named in the refusal's criteria.

        Raises:
            PurposeRequired: The key's audit action is one the call must state a purpose for, and it stated none.
                The ``denied`` event is written first, with no target.
            KeyError: If ``key`` is not in :data:`OPERATIONS`.
            AuditWriteError: If the ``denied`` event cannot be written.
        """
        binding = self._binding
        if binding is None:
            return
        action, _ = OPERATIONS[key]
        if action not in binding.required or stated(binding.purpose):
            return
        criteria = {"refused": "purpose_required", "container": f"chat:{p.chat_id}"}
        self.record(key, p, outcome="denied", target_kind=None, criteria=criteria)
        raise PurposeRequired

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
        target_kind: str | None = _FILE,
        count: int | None = None,
        request_id: str | None = None,
    ) -> str:
        """Record one file or tool action of a user, by file id, or by count for data that came with the call.

        Args:
            key: The action, a key of :data:`OPERATIONS`.
            p: The caller, the user and the chat folder of the files the event names; for an
                action on a file that is the folder the file lives in, never the chat of the request.
            file_ids: Ids of the files the action touched; never names.
            outcome: ``ok``, ``denied`` or ``error``.
            criteria: The action's parameters, never content. ``caller`` is
                added, and replaces a ``caller`` given here.
            purpose: The stated purpose, if any; a :meth:`bound` log's when ``None``.
            target_kind: What the action touched, ``file`` by default; ``None`` writes no target, as for a call
                refused before it touched anything.
            count: How many objects it touched, in place of ``len(file_ids)``.
            request_id: The call's request id, on the event and on each continuation; a :meth:`bound` log's when
                ``None``.

        Returns:
            The id of the written event; more than 200 ids add continuations (:meth:`_emit`).

        Raises:
            KeyError: If ``key`` is not in :data:`OPERATIONS`.
            AuditWriteError: If the event or a continuation cannot be written.
            RuntimeError: If the chain is not started or already closed.
        """
        action, operation = OPERATIONS[key]
        if self._binding is not None:
            purpose = self._binding.purpose if purpose is None else purpose
            request_id = self._binding.request_id if request_id is None else request_id
        # caller goes first so it is among the criteria the emitter keeps when it caps their number.
        merged: dict[str, object] = {"caller": p.caller}
        merged.update((name, value) for name, value in (criteria or {}).items() if name != "caller")
        target = None
        if target_kind is not None:
            touched = len(file_ids) if count is None else count
            target = Target(kind=target_kind, ids=file_ids, count=touched, container=f"chat:{p.chat_id}")
        return self._emit(
            action,
            operation,
            Actor(user=p.user_id, via="service"),
            outcome=outcome,
            purpose=purpose,
            target=target,
            criteria=merged,
            request_id=request_id,
        )

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
            The id of the written event; more than 200 ids add continuations (:meth:`_emit`).

        Raises:
            ValueError: If ``action`` or ``outcome`` is unknown.
            AuditWriteError: If the event or a continuation cannot be written.
            RuntimeError: If the chain is not started or already closed.
        """
        target = None
        if file_ids or count is not None or container is not None:
            target = Target(
                kind=_FILE, ids=file_ids, count=len(file_ids) if count is None else count, container=container
            )
        return self._emit(action, operation, _SYSTEM_ACTOR, outcome=outcome, target=target, criteria=criteria)

    def _emit(
        self,
        action: str,
        operation: str,
        actor: Actor,
        *,
        outcome: str,
        target: Target | None,
        criteria: Mapping[str, object] | None,
        purpose: Purpose | None = None,
        request_id: str | None = None,
    ) -> str:
        """Write an event, and a continuation for each further 200 of its ids, so that no id is cut.

        The event carries the first 200 ids and the target's full count. Each continuation is ``<operation>.ids``, with
        ``parent_event_id`` the event's id, the same action, actor, outcome, purpose, request id and container,
        ``count`` the number of its own ids, and no criteria.

        Args:
            action: The audit action.
            operation: The event's operation.
            actor: Who acted.
            outcome: ``ok``, ``denied`` or ``error``.
            target: What it touched, every id given; ``None`` for no target.
            criteria: The event's criteria.
            purpose: The stated purpose, if any.
            request_id: The call's request id, if any.

        Returns:
            The id of the event.

        Raises:
            AuditWriteError: If the event or a continuation cannot be written; the events before it stay written.
        """
        if target is None:
            event = self.emitter.emit(
                action, operation, actor, outcome=outcome, request_id=request_id, purpose=purpose, criteria=criteria
            )
            return str(event["event_id"])
        chunks = id_chunks([str(item) for item in target.ids])
        count = len(target.ids) if target.count is None else target.count
        first = Target(kind=target.kind, ids=chunks[0] if chunks else (), count=count, container=target.container)
        event = self.emitter.emit(
            action,
            operation,
            actor,
            outcome=outcome,
            request_id=request_id,
            purpose=purpose,
            target=first,
            criteria=criteria,
        )
        event_id = str(event["event_id"])
        for chunk in chunks[1:]:
            self.emitter.emit(
                action,
                operation + CONTINUATION,
                actor,
                outcome=outcome,
                request_id=request_id,
                parent_event_id=event_id,
                purpose=purpose,
                target=Target(kind=target.kind, ids=chunk, count=len(chunk), container=target.container),
            )
        return event_id

    def close(self) -> None:
        """End the chain with its ``shutdown`` event; later calls do nothing.

        Raises:
            AuditWriteError: If the shutdown event could not be written.
        """
        self.emitter.close()
