"""Test-only: a stand-in OpenAI-compatible model for the contract stack (contract/compose.yaml); never deployed.

It serves one model, ``stub-model``. Asked for a chat completion with tools, it asks for one ``read_document`` call on
the first ``/workspace/...`` path in the last user message; once a tool's answer is in the conversation, it answers in
plain text. Every request it gets is one JSON line on stdout, for tests/contract to read: the chat it was for (Open
WebUI's ``X-OpenWebUI-Chat-Id``), the tools offered, the system message, and the tool answers the request carried.
Standard library only: it runs in the workbench api's image, so nothing is pulled.
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final, override

MODEL: Final = "stub-model"
ANSWER: Final = "The stub model read the file."
#: A workspace path in a sentence: it ends on a letter, a digit or ``_``, never on the sentence's own ``?`` or ``.``.
_PATH: Final = re.compile(r"/workspace/[A-Za-z0-9_.:/-]*[A-Za-z0-9_]")


def _text(content: Any) -> str:
    """Flatten a message's content, a string or a list of parts, into its text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def reply(request: dict[str, Any]) -> dict[str, Any]:
    """Choose the assistant's message: one ``read_document`` call, or plain text once a tool has answered.

    Args:
        request: The chat completion request.

    Returns:
        The message, as the ``message`` of an OpenAI choice.
    """
    messages = request.get("messages") or []
    tools = [tool["function"]["name"] for tool in request.get("tools") or []]
    last: dict[str, Any] = messages[-1] if messages else {}
    found = _PATH.search(_text(last.get("content"))) if last.get("role") == "user" else None
    if found is None or "read_document" not in tools:
        return {"role": "assistant", "content": ANSWER}
    call = {"name": "read_document", "arguments": json.dumps({"path": found.group(0)})}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_1", "type": "function", "function": call}],
    }


class Handler(BaseHTTPRequestHandler):
    """Answers ``GET /v1/models`` and ``POST /v1/chat/completions``, streamed or not."""

    @override
    def log_message(self, format: str, *args: Any) -> None:
        """Keep the default access log off stderr."""

    def _json(self, status: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        """List the one model; anything else is not found."""
        if self.path.rstrip("/").endswith("/models"):
            self._json(
                200, {"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "stub"}]}
            )
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        """Answer a chat completion and write down what it was given."""
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        messages = request.get("messages") or []
        print(
            json.dumps(
                {
                    "chat": self.headers.get("X-OpenWebUI-Chat-Id"),
                    "tools": [tool["function"]["name"] for tool in request.get("tools") or []],
                    "system": "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system"),
                    "tool_answers": [_text(m.get("content")) for m in messages if m.get("role") == "tool"],
                }
            ),
            flush=True,
        )
        message = reply(request)
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        base = {"id": "chatcmpl-stub", "created": int(time.time()), "model": MODEL}
        if not request.get("stream"):
            choice = {"index": 0, "message": message, "finish_reason": finish}
            self._json(200, {**base, "object": "chat.completion", "choices": [choice]})
            return
        if message.get("tool_calls"):
            delta = {"role": "assistant", "tool_calls": [{"index": 0, **message["tool_calls"][0]}]}
        else:
            delta = {"role": "assistant", "content": message["content"]}
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for choice in (
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": dict[str, Any](), "finish_reason": finish},
        ):
            chunk = {**base, "object": "chat.completion.chunk", "choices": [choice]}
            self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def main() -> None:
    """Serve on port 9000 until stopped."""
    print(f"stub model {MODEL} on :9000", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()


if __name__ == "__main__":
    main()
