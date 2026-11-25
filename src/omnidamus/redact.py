"""Keep customer identifiers out of logs.

A WhatsApp message ID (wamid) is base64 and decodes to text that includes the
customer's phone number, so it is logged only as a hash.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_IDENTIFIER = re.compile(r"wamid\.\S+|[^@\s]+@[^@\s]+\.[^@\s]+|\+?\d{7,}")


def message_ref(message_id: str | None) -> str | None:
    """A loggable reference: AWS's own IDs as they are, a wamid as a short hash."""

    if message_id and message_id.startswith("wamid."):
        return "wamid-sha256:" + hashlib.sha256(message_id.encode()).hexdigest()[:12]
    return message_id


def redact(value: Any) -> Any:
    """`value` with wamids, email addresses and phone-length numbers removed from its text."""

    if isinstance(value, str):
        return _IDENTIFIER.sub("<redacted>", value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value
