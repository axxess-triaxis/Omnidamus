"""Builds the Strands Agent that reasons about cross-channel conversation routing.

Uses the Strands Agents SDK on Amazon Bedrock -- the hackathon FAQ confirms this
is an accepted alternative to Bedrock AgentCore. `structured_output_model` forces
the model's reply into `RoutingDecision`, so `router.py` never has to parse prose.
"""

from __future__ import annotations

import os

import boto3
from strands import Agent
from strands.models import BedrockModel

from .schema import InboundMessage, RoutingDecision
from .state import ConversationState

SYSTEM_PROMPT = """You are Omnidamus, an agent that carries one customer conversation across \
multiple channels -- SMS/RCS, WhatsApp, and email -- deciding at each inbound message what \
should happen next.

You will be handed the text of one inbound message and which channel it arrived on. Decide:

- reply_channel: which channel to reply on. Usually the same channel the message arrived on, \
  unless a handoff is in progress.
- reply_text: the exact text to send back.
- handoff_to: set only when the conversation should move to a different channel next -- for \
  example, an initial SMS/RCS notification that needs real back-and-forth belongs on WhatsApp; \
  a WhatsApp exchange that has reached a firm outcome belongs on email as a written receipt. \
  Leave unset when the conversation should continue on its current channel.
- conversation_complete: true only when the exchange has reached a final, actionable outcome \
  (a confirmed time, a resolved request, an explicit decline) -- not merely when the user stops \
  responding.
- key_facts: concrete facts actually present in the message -- names, dates, amounts, decisions \
  -- never invented. If the customer gives an email address, put it in key_facts["email"].

You are also given the conversation so far: its purpose, the facts already known, the recent \
turns on every channel, whether an email address is on file, and whether WhatsApp can be used \
right now. WhatsApp only allows free-text replies within 24 hours of the customer's own last \
WhatsApp message; when it is closed, do not reply on WhatsApp -- hand off to WhatsApp instead \
and the system will send the customer a link to start the chat.

The closing receipt goes by email. Before setting conversation_complete, make sure an email \
address is on file; if none is, ask for one first. Customers can also reply to that email, so a \
message may arrive on the email channel; answer it there unless another channel suits better.

Opt-outs (STOP, UNSUBSCRIBE, ...) are handled before you see a message; anything you receive is \
from a customer who is still opted in.

Be concrete: pull real details out of the message rather than describing it in the abstract."""


def get_model() -> BedrockModel:
    region = os.environ.get("BEDROCK_REGION", "us-east-1")
    session = boto3.Session(profile_name=os.environ.get("AWS_PROFILE"), region_name=region)
    return BedrockModel(
        model_id=os.environ.get(
            "OMNIDAMUS_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6"
        ),
        boto_session=session,
        temperature=0.2,
    )


def build_agent() -> Agent:
    """A fresh agent per message. Strands agents keep their own chat history, so
    reusing one across conversations would leak one customer's messages into
    another's; the conversation's real history comes from DynamoDB instead."""

    return Agent(model=get_model(), system_prompt=SYSTEM_PROMPT)


def build_prompt(
    message: InboundMessage,
    state: ConversationState | None = None,
    whatsapp_window_open: bool = False,
) -> str:
    lines = [
        f"Channel: {message.channel.value}",
        f"From: {message.from_address}",
        f"Conversation ID: {message.conversation_id}",
    ]
    if state is not None:
        lines.append(f"Purpose: {state.context or 'not given'}")
        lines.append(f"Email on file: {'yes' if state.customer_email else 'no'}")
        lines.append(f"WhatsApp open for free-text replies: {'yes' if whatsapp_window_open else 'no'}")
        if state.key_facts:
            facts = "; ".join(f"{k}={v}" for k, v in state.key_facts.items())
            lines.append(f"Known facts: {facts}")
        if state.turns:
            lines.append("\nConversation so far (oldest first):")
            for turn in state.turns:
                who = "Customer" if turn.direction == "inbound" else "Omnidamus"
                lines.append(f"[{turn.channel.value}] {who}: {turn.text}")
    lines.append(f"\nNew message:\n{message.body}")
    return "\n".join(lines)


def route_message(
    message: InboundMessage,
    state: ConversationState | None = None,
    whatsapp_window_open: bool = False,
) -> RoutingDecision:
    agent = build_agent()
    response = agent(
        build_prompt(message, state, whatsapp_window_open),
        structured_output_model=RoutingDecision,
    )
    return response.structured_output
