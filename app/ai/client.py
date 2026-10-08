"""Workers AI REST client.

One POST per call, 20 s timeout, one retry on a 5xx or network error. The documented
"JSON Mode couldn't be met" error is a normal skip, not a failure. The token and the
account URL are never logged or put in an exception message.
"""

import json
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import Connection

from app.core.config import Settings
from app.services import secrets

log = structlog.get_logger()

API = "https://api.cloudflare.com/client/v4"
TIMEOUT_SECONDS = 20.0
JSON_MODE_FAILED = "JSON Mode couldn't be met"


class AiSkip(Exception):
    """The call produced nothing usable, by design (e.g. JSON Mode couldn't be met)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AiError(Exception):
    """The call failed (HTTP error, timeout, malformed body). `reason` is safe to store."""

    def __init__(self, reason: str, raw: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


@dataclass(frozen=True, slots=True)
class Result:
    data: Any  # dict for JSON Mode, str for free text
    input_tokens: int | None
    output_tokens: int | None
    raw: str


class WorkersAI:
    def __init__(
        self,
        account_id: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        self._account_id = account_id
        self._token = token
        self._transport = transport
        self._timeout = timeout

    def __repr__(self) -> str:  # never show the token
        return "WorkersAI(<configured>)"

    def run(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        json_schema: dict[str, Any] | None = None,
    ) -> Result:
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_tokens}
        if json_schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": json_schema}
        response = self._post(model, body)
        raw = response.text[:8192]
        try:
            envelope = response.json()
        except ValueError as exc:
            raise AiError("malformed_envelope", raw) from exc
        if response.status_code >= 400 or not envelope.get("success", False):
            messages_text = json.dumps(envelope.get("errors", []))
            if JSON_MODE_FAILED in messages_text:
                raise AiSkip("json_mode")
            raise AiError(f"http_{response.status_code}", raw)
        result = envelope.get("result") or {}
        output = result.get("response")
        usage = result.get("usage") or {}
        if json_schema is not None:
            if isinstance(output, str):
                try:
                    output = json.loads(output)
                except ValueError as exc:
                    raise AiError("malformed_json", raw) from exc
            if not isinstance(output, dict):
                raise AiError("malformed_json", raw)
        elif not isinstance(output, str):
            raise AiError("malformed_text", raw)
        return Result(
            data=output,
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            raw=raw,
        )

    def _post(self, model: str, body: dict[str, Any]) -> httpx.Response:
        url = f"{API}/accounts/{self._account_id}/ai/run/{model}"
        headers = {"Authorization": f"Bearer {self._token}"}
        last = "network"
        with httpx.Client(transport=self._transport, timeout=self._timeout) as client:
            for attempt in (1, 2):
                try:
                    response = client.post(url, json=body, headers=headers)
                except httpx.TimeoutException:
                    last = "timeout"
                except httpx.HTTPError:
                    last = "network"
                else:
                    if response.status_code < 500:
                        return response
                    last = f"http_{response.status_code}"
                log.warning("ai_call_retry" if attempt == 1 else "ai_call_failed", reason=last)
        raise AiError(last)


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def from_store(
    conn: Connection, settings: Settings, transport: httpx.BaseTransport | None = None
) -> WorkersAI | None:
    """The configured client, or None without an account id and token (or a usable key)."""
    key = settings.app_secret_key.get_secret_value()
    account_id = secrets.get(conn, key, "workers_ai.account_id")
    token = secrets.get(conn, key, "workers_ai.token")
    if not account_id or not token:
        return None
    return WorkersAI(account_id, token, transport=transport)
