"""The compose healthcheck's route: ``/healthz``, open to anyone (:data:`~workbench.api.auth.OPEN_PATHS`).

It answers from memory, and it and its route are ``async``, so a burst of uploads that holds every worker thread
cannot delay it.
"""

from fastapi import APIRouter

from workbench.api.routing import EncodableRoute

router = APIRouter(include_in_schema=False, route_class=EncodableRoute)


@router.get("/healthz")
async def healthz() -> dict[str, bool]:
    """Answer the compose healthcheck. It needs no key and is not in the schema.

    Returns:
        ``{"ok": true}``.
    """
    return {"ok": True}
