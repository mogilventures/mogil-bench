from __future__ import annotations

import json
import math
import re
from typing import Any, Protocol
from urllib.error import HTTPError, URLError

DEFAULT_UPLOAD_TIMEOUT_SECONDS = 120.0
MAX_UPLOAD_TIMEOUT_SECONDS = 600.0
MAX_RESPONSE_BYTES = 65_536
MAX_HTTP_DIAGNOSTIC_BYTES = 4_096
MAX_HTTP_DIAGNOSTIC_CHARACTERS = 300

_AUTHORIZATION = re.compile(r"(?i)authorization\s*[:=]\s*(?:bearer\s+)?[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|api|key|token|secret)[-_][A-Za-z0-9._-]{12,}\b")
_ABSOLUTE_PATH = re.compile(
    r"(?<![:A-Za-z0-9_.-])/(?!/)(?:[A-Za-z0-9_.@+-]+/)*[A-Za-z0-9_.@+-]+"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


class _Readable(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


class UploadResponseError(ValueError):
    pass


def validate_upload_timeout(value: float) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0 or timeout > MAX_UPLOAD_TIMEOUT_SECONDS:
        raise ValueError(
            f"upload timeout must be finite, positive, and at most "
            f"{MAX_UPLOAD_TIMEOUT_SECONDS:g} seconds"
        )
    return timeout


def read_json_response(response: _Readable) -> Any:
    data = response.read(MAX_RESPONSE_BYTES + 1)
    if len(data) > MAX_RESPONSE_BYTES:
        raise UploadResponseError("server counts response exceeds the safe size limit")
    try:
        return json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise UploadResponseError("server returned a malformed JSON counts response") from error


def is_timeout_error(error: BaseException) -> bool:
    if isinstance(error, TimeoutError):
        return True
    return isinstance(error, URLError) and isinstance(error.reason, TimeoutError)


def timeout_diagnostic(label: str) -> str:
    return (
        f"{label} timed out; outcome is unknown and the request may have completed. "
        "Do not retry automatically. Check the destination state, then retry the exact same "
        "artifact if needed; stable IDs make that retry idempotent."
    )


def _request_strings(body: bytes) -> set[str]:
    try:
        value = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return set()
    strings: set[str] = set()

    def visit(item: Any) -> None:
        if isinstance(item, str):
            if item:
                strings.add(item)
        elif isinstance(item, list):
            for child in item:
                visit(child)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)

    visit(value)
    return strings


def _diagnostic_value(data: bytes) -> str | None:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        value = None
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("code", "error", "message", "detail"):
            item = value.get(key)
            if isinstance(item, (str, int)) and not isinstance(item, bool):
                parts.append(f"{key}={item}")
        return "; ".join(parts) or None
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None
    return text if text.strip() else None


def http_error_diagnostic(error: HTTPError, *, body: bytes, token: str) -> str:
    try:
        response_data = error.read(MAX_HTTP_DIAGNOSTIC_BYTES + 1)
    except (AttributeError, OSError):
        response_data = b""
    diagnostic = _diagnostic_value(response_data[:MAX_HTTP_DIAGNOSTIC_BYTES])
    suffix = ""
    if diagnostic:
        for secret in sorted({token, *_request_strings(body)}, key=len, reverse=True):
            if secret:
                diagnostic = diagnostic.replace(secret, "[REDACTED]")
        diagnostic = _AUTHORIZATION.sub("[REDACTED]", diagnostic)
        diagnostic = _SECRET_ASSIGNMENT.sub("[REDACTED]", diagnostic)
        diagnostic = _SECRET_TOKEN.sub("[REDACTED]", diagnostic)
        diagnostic = _ABSOLUTE_PATH.sub("[PATH]", diagnostic)
        diagnostic = " ".join(_CONTROL.sub(" ", diagnostic).split())
        if len(diagnostic) > MAX_HTTP_DIAGNOSTIC_CHARACTERS:
            diagnostic = diagnostic[:MAX_HTTP_DIAGNOSTIC_CHARACTERS].rstrip() + "…"
        if diagnostic:
            suffix = f": {diagnostic}"
    return f"HTTP {error.code}{suffix}"
