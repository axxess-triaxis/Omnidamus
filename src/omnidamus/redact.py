"""Keep customer identifiers out of logs.

A WhatsApp message ID (wamid) is base64 and decodes to text that includes the
customer's phone number, so it is logged only as a hash.
"""

from __future__ import annotations

import hashlib
import re
import traceback
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

_IDENTIFIER = re.compile(
    r"wamid\.\S+|[^@\s]+@[^@\s]+\.[^@\s]+|\+\d[\d\s().-]{5,}\d|\+?\d{7,}")


def message_ref(message_id: str | None) -> str | None:
    """A loggable reference: AWS's own IDs as they are, a wamid as a short hash."""

    if message_id and message_id.startswith("wamid."):
        return "wamid-sha256:" + hashlib.sha256(message_id.encode()).hexdigest()[:12]
    return message_id


def error_outline(exc: BaseException) -> dict:
    """What an unhandled error was and where, without what the customer wrote.

    The message is kept only for Omnidamus's own errors (redacted), which carry AWS's reason
    or fixed text. Any other message may quote the input, as pydantic's does, so for those
    only the type, and AWS's error code if there is one, is kept."""

    outline: dict = {"error": type(exc).__name__}
    if type(exc).__module__.startswith("omnidamus"):
        outline["message"] = redact(str(exc))[:300]
    caused_by = []
    link: BaseException | None = exc
    while link is not None and len(caused_by) < 5:
        if link is not exc:
            caused_by.append(type(link).__name__)
        if isinstance(link, ClientError):
            outline.setdefault("aws_code", link.response.get("Error", {}).get("Code"))
            outline.setdefault("aws_operation", link.operation_name)
        link = link.__cause__
    if caused_by:
        outline["caused_by"] = caused_by
    frames = traceback.extract_tb(exc.__traceback__)
    outline["at"] = [f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in frames[-10:]]
    return outline


def redact(value: Any) -> Any:
    """`value` with wamids, email addresses and phone-length numbers removed from its text."""

    if isinstance(value, str):
        return _IDENTIFIER.sub("<redacted>", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
