"""A local stand-in for Discord's webhook endpoint: records every request and answers
with scripted responses (default 204). Used by the dispatcher tests and, via
`python -m tests.fake_discord`, as the fake receiver for local end-to-end runs."""

import json
import sys
import threading
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class FakeDiscord:
    base: str = ""
    requests: list[dict[str, Any]] = field(default_factory=list)
    script: deque[tuple[int, dict[str, Any] | None]] = field(default_factory=deque)

    def url(self, webhook: str = "1/token") -> str:
        return f"{self.base}/api/webhooks/{webhook}"

    def respond(self, *responses: tuple[int, dict[str, Any] | None]) -> None:
        self.script.extend(responses)

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests]


def _handler(fake: FakeDiscord) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            fake.requests.append({"path": self.path, "body": body})
            status, reply = fake.script.popleft() if fake.script else (204, None)
            payload = json.dumps(reply).encode() if reply is not None else b""
            self.send_response(status)
            if payload:
                self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass  # the script below prints each post itself

    return Handler


@contextmanager
def running(port: int = 0) -> Iterator[FakeDiscord]:
    fake = FakeDiscord()
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(fake))
    fake.base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":  # fake receiver for local runs: prints each post's title
    with running(int(sys.argv[1]) if len(sys.argv) > 1 else 8099) as fake:
        print(f"fake Discord listening on {fake.base}", flush=True)
        seen = 0
        try:
            while True:
                threading.Event().wait(1)
                for r in fake.requests[seen:]:
                    embed = (r["body"].get("embeds") or [{}])[0]
                    print(
                        f"{r['path']}: {embed.get('title')} | {embed.get('description')}",
                        flush=True,
                    )
                seen = len(fake.requests)
        except KeyboardInterrupt:
            pass
