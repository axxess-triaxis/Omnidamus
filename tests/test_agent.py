from __future__ import annotations

from unittest.mock import patch

import pytest

from omnidamus import agent
from omnidamus.schema import Channel, InboundMessage
from omnidamus.state import ConversationState


def test_agent_does_not_print_model_output():
    """Strands prints the model's text by default, which puts customer messages in CloudWatch."""
    with patch.object(agent, "get_model"), patch.object(agent, "Agent") as build:
        agent.build_agent()
    assert build.call_args.kwargs["callback_handler"] is None


@pytest.mark.parametrize("receipt_id", [None, "ses-1"])
def test_agent_is_told_whether_a_receipt_went_out(receipt_id):
    message = InboundMessage(conversation_id="+15550001111", channel=Channel.WHATSAPP,
                             from_address="+15550001111", body="Send it again?")
    state = ConversationState(conversation_id="+15550001111", customer_phone="+15550001111",
                              receipt_message_id=receipt_id)
    assert ("Receipt: already emailed" in agent.build_prompt(message, state)) is bool(receipt_id)
