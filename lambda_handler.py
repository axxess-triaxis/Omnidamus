"""AWS Lambda entry point.

Two kinds of event reach this function, and no public HTTP endpoint is needed for
either:

1. SNS records from the two inbound topics -- AWS End User Messaging Social
   (WhatsApp) and AWS End User Messaging SMS (two-way SMS). Every record is checked
   before it is parsed: its TopicArn must be one of the two configured topics and
   its SNS signature must verify (sns_verify.py).
2. A direct invocation {"action": "start_conversation", ...} to open a conversation
   with an outbound SMS/RCS notification. Only callers with lambda:InvokeFunction
   on this function can send it (see scripts/start_conversation.py).
"""

from __future__ import annotations

import json
import logging
import os

from omnidamus import inbound, service, sns_verify
from omnidamus.state import ConversationStore

logging.getLogger().setLevel(logging.INFO)
log = logging.getLogger(__name__)

_store: ConversationStore | None = None


def _get_store() -> ConversationStore:
    global _store
    if _store is None:
        _store = ConversationStore()
    return _store


def _topics() -> dict[str, str]:
    return {
        os.environ.get("OMNIDAMUS_WHATSAPP_TOPIC_ARN", ""): "whatsapp",
        os.environ.get("OMNIDAMUS_SMS_TOPIC_ARN", ""): "sms",
    }


def _handle_sns_record(record: dict) -> list[dict]:
    sns = record["Sns"]
    topics = _topics()
    sns_verify.verify(sns, allowed_topic_arns=topics.keys())

    kind = topics[sns["TopicArn"]]
    parse = inbound.parse_whatsapp_event if kind == "whatsapp" else inbound.parse_sms_event
    results = []
    for message in parse(sns["Message"]):
        results.append(service.handle_inbound(message, _get_store()))
    return results


def handler(event: dict, context=None) -> dict:
    if event.get("action") == "start_conversation":
        return service.start_conversation(
            phone=inbound.normalize_phone(event["phone"]),
            notification=event["notification"],
            store=_get_store(),
            email=event.get("email"),
            context=event.get("context", ""),
        )

    records = [r for r in event.get("Records", []) if r.get("EventSource") == "aws:sns"]
    if not records:
        raise ValueError("Unrecognized event")

    results = []
    for record in records:
        try:
            results.extend(_handle_sns_record(record))
        except sns_verify.SnsVerificationError as exc:
            # Not ours or tampered with: log and drop, never retry.
            log.warning("Rejected SNS record: %s", exc)
    log.info(json.dumps({"processed": len(results)}))
    return {"results": results}
