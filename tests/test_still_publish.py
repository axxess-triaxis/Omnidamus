"""Completed conversations are published to STILL, after the save, at most once per
closing message, and a STILL failure never affects the customer."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from omnidamus import service, still_emit
from omnidamus.schema import Channel, RoutingDecision
from tests.test_service import NOW, PHONE, Recorder, _msg

SCHEMA = json.loads((Path(__file__).parent / "fixtures" / "still_conversation.completed.v1.json")
                    .read_text(encoding="utf-8"))
BUS = "arn:aws:events:ap-southeast-2:123456789012:event-bus/still"

DONE = RoutingDecision(summary="Demo booked for tomorrow 6 pm IST", reply_channel=Channel.WHATSAPP,
                       reply_text="Confirmed.", conversation_complete=True,
                       key_facts={"demo_time": "tomorrow, 6 pm IST", "email": "cust@example.com"})


class FakeEvents:
    def __init__(self, error=None):
        self.entries = []
        self.error = error

    def put_events(self, Entries):
        if self.error:
            raise self.error
        self.entries.extend(Entries)
        return {"FailedEntryCount": 0, "Entries": [{"EventId": "e"}]}


@pytest.fixture
def rec():
    return Recorder()


@pytest.fixture
def events(monkeypatch):
    monkeypatch.setenv(still_emit.BUS_ENV, BUS)
    return FakeEvents()


def _handle(store, rec, events, message, decision, store_seen=None):
    def publish(*args, **kwargs):
        if store_seen is not None:
            store_seen.append(store.get(PHONE))
        return still_emit.emit(*args, client=events, **kwargs)

    return service.handle_inbound(message, store, route=lambda *_: decision, send=rec.send,
                                  send_email=rec.send_email, send_template=rec.send_template,
                                  now=NOW, publish=publish)


def test_completion_publishes_one_event_matching_still_schema(store, rec, events):
    service.start_conversation(PHONE, store, notification="Hi", context="Inbound lead", send=rec.send)
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "6 pm works, cust@example.com", "wamid.9"), DONE)

    [entry] = events.entries
    assert entry["Source"] == "triaxis.omnidamus"
    assert entry["DetailType"] == "conversation.completed.v1"
    data = json.loads(entry["Detail"])["data"]
    jsonschema.validate(data, SCHEMA)
    assert data["context"] == "Inbound lead"
    assert data["key_facts"] == {"demo_time": "tomorrow, 6 pm IST"}
    assert data["customer_email"] == "cust@example.com"
    assert data["receipt_sent"] is True
    assert data["completed_at"] == "2026-10-25T10:00:00Z"


def test_publish_happens_after_the_state_is_saved(store, rec, events):
    seen = []
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "6 pm", "wamid.9"), DONE, store_seen=seen)
    assert seen[0].status == "complete"


def test_open_conversation_publishes_nothing(store, rec, events):
    ongoing = RoutingDecision(summary="asks time", reply_channel=Channel.WHATSAPP, reply_text="What time?")
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "hi", "wamid.1"), ongoing)
    assert events.entries == []


def test_event_id_is_stable_per_closing_message(store, rec, events):
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "6 pm", "wamid.9"), DONE)
    # A later exchange that completes again is a new event.
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "and Friday?", "wamid.10"), DONE)
    first, second = (json.loads(e["Detail"])["id"] for e in events.entries)
    assert first != second
    assert first == still_emit.envelope("triaxis.omnidamus", "conversation.completed.v1", {},
                                        key=f"{PHONE}:wamid.9")["id"]


def test_still_failure_does_not_affect_the_customer(store, rec, events, caplog):
    events.error = RuntimeError(f"bus down for {PHONE}")
    result = _handle(store, rec, events, _msg(Channel.WHATSAPP, "6 pm", "wamid.9"), DONE)
    assert result["status"] == "complete"
    assert rec.sent[-1] == (Channel.WHATSAPP, PHONE, "Confirmed.")
    assert store.get(PHONE).status == "complete"
    assert "STILL publish failed: RuntimeError" in caplog.text
    assert PHONE not in caplog.text


def test_nothing_is_published_without_the_bus_setting(store, rec, monkeypatch):
    monkeypatch.delenv(still_emit.BUS_ENV, raising=False)
    events = FakeEvents()
    _handle(store, rec, events, _msg(Channel.WHATSAPP, "6 pm", "wamid.9"), DONE)
    assert events.entries == []
