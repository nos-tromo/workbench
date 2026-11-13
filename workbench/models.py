"""Value types shared across the api."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    """Who is calling and which chat folder their files go to by default.

    The user is the access boundary: every file lives under the user's own root.
    The chat id is only a folder inside that root and never selects another
    user's data, whatever value a client sends.

    Attributes:
        caller: Authenticated calling service, for example ``open-webui``.
        user_id: The caller's own id for the end user.
        chat_id: Chat folder used when a path names none; ``default`` without a session.
    """

    caller: str
    user_id: str
    chat_id: str
