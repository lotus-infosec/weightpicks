"""A tiny in-process SMTP server for tests (plain text, no TLS): records each message.
Python 3.12 removed smtpd, and the app needs nothing more than this to be tested."""

import socketserver
import threading
from dataclasses import dataclass, field
from email import message_from_bytes
from email.message import Message


@dataclass
class Inbox:
    messages: list[Message] = field(default_factory=list)
    reject_auth: bool = False

    def bodies(self) -> list[str]:
        out = []
        for m in self.messages:
            payload = m.get_payload(decode=True)
            out.append(payload.decode() if isinstance(payload, bytes) else str(payload))
        return out


class _Handler(socketserver.StreamRequestHandler):
    inbox: Inbox

    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self) -> None:
        self.reply("220 fake ESMTP")
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            cmd = raw.decode(errors="replace").strip()
            upper = cmd.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.reply("250-fake")
                self.reply("250 AUTH PLAIN LOGIN")
            elif upper.startswith("AUTH"):
                self.reply("535 bad credentials" if self.inbox.reject_auth else "235 ok")
            elif upper.startswith(("MAIL", "RCPT", "RSET", "NOOP")):
                self.reply("250 ok")
            elif upper == "DATA":
                self.reply("354 go ahead")
                lines = []
                while (line := self.rfile.readline()) not in (b".\r\n", b".\n", b""):
                    lines.append(line[1:] if line.startswith(b"..") else line)
                self.inbox.messages.append(message_from_bytes(b"".join(lines)))
                self.reply("250 queued")
            elif upper == "QUIT":
                self.reply("221 bye")
                return
            else:
                self.reply("502 not implemented")


class FakeSmtp:
    def __init__(self) -> None:
        self.inbox = Inbox()
        handler = type("Handler", (_Handler,), {"inbox": self.inbox})
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
