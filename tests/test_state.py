from __future__ import annotations

import pytest

from omnidamus.schema import Channel
from omnidamus.state import MAX_TURNS, ConversationConflictError, ConversationState


def test_round_trip(store):
    state = ConversationState(conversation_id="+15550001111", customer_phone="+15550001111")
    state.add_turn("inbound", Channel.SMS_RCS, "hello")
    state.key_facts["time"] = "3pm"
    store.save(state)

    loaded = store.get("+15550001111")
    assert loaded.version == 1
    assert loaded.turns[0].text == "hello"
    assert loaded.key_facts == {"time": "3pm"}


def test_missing_conversation_is_none(store):
    assert store.get("+19999999999") is None


def test_stale_write_is_rejected(store):
    store.save(ConversationState(conversation_id="+1555", customer_phone="+1555"))
    first = store.get("+1555")
    second = store.get("+1555")

    first.context = "first writer"
    store.save(first)

    second.context = "second writer"
    with pytest.raises(ConversationConflictError):
        store.save(second)
    assert store.get("+1555").context == "first writer"


def test_new_conversation_cannot_overwrite_existing(store):
    store.save(ConversationState(conversation_id="+1555", customer_phone="+1555"))
    with pytest.raises(ConversationConflictError):
        store.save(ConversationState(conversation_id="+1555", customer_phone="+1555"))


def test_turns_and_processed_ids_are_bounded():
    state = ConversationState(conversation_id="x", customer_phone="x")
    for i in range(MAX_TURNS + 5):
        state.add_turn("inbound", Channel.WHATSAPP, str(i))
        state.mark_processed(f"m{i}")
    assert len(state.turns) == MAX_TURNS
    assert state.turns[-1].text == str(MAX_TURNS + 4)
    assert state.already_processed(f"m{MAX_TURNS + 4}")
