"""Staging outbound capture: when OMNIDAMUS_OUTBOUND_MODE is "capture", channels
record that a message would have gone out instead of sending it, so an end-to-end
test can run the whole conversation without reaching a real phone or inbox.

Only the staging stack sets this variable (template.yaml). Addresses and message
text are not logged.
"""

from __future__ import annotations

import json
import logging
import os
import uuid

log = logging.getLogger(__name__)

MESSAGE_ID_PREFIX = "captured-"


def enabled() -> bool:
    return os.environ.get("OMNIDAMUS_OUTBOUND_MODE") == "capture"


def record(channel: str) -> str:
    """Log the capture and return a message ID in place of the provider's."""

    message_id = f"{MESSAGE_ID_PREFIX}{uuid.uuid4()}"
    log.info(json.dumps({"outbound_captured": channel, "message_id": message_id}))
    return message_id
