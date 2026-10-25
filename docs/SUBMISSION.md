# Devpost submission draft

Draft text for the AWS CDS Agentic AI Partner Hackathon form. Fields marked
**TODO** need a deployed stack or the entrant's own details before submitting.

## Name and tagline

**Omnidamus** — one AI agent that carries a customer conversation across SMS,
WhatsApp and email, and closes it with a written receipt.

## The problem

Customer conversations don't stay in one channel. A pickup notice goes out by
SMS, the customer wants to reschedule over WhatsApp, and the confirmation belongs
in email. Most businesses run these as three disconnected integrations. The
customer repeats themselves, the context is lost at every switch, and nobody owns
the outcome.

## What it does

Omnidamus treats the three channels as one conversation:

- It opens with an SMS/RCS notification or an approved WhatsApp template.
- An agent on Amazon Bedrock reads the whole history on every message and
  decides where the conversation should go next. That can be a reply on the same
  channel, a move to WhatsApp for real back-and-forth, or email when there's an
  outcome to put in writing.
- When the exchange reaches an outcome, the customer gets an SES receipt, and
  replying to that email continues the same conversation.
- Compliance rules run in code, not in the model: WhatsApp's 24-hour messaging
  window is enforced, and STOP on any channel ends all contact.

## How it's built

- **AWS End User Messaging SMS** (`pinpoint-sms-voice-v2`): outbound SMS/RCS, and
  two-way inbound SMS via SNS.
- **AWS End User Messaging Social** (`socialmessaging`): WhatsApp text and
  template messages, with inbound messages and delivery receipts via SNS.
- **Amazon SES** (`sesv2`, `ses`): closing receipts, plus inbound email replies
  via a receipt rule and SNS.
- **Amazon Bedrock** through the Strands Agents SDK: the agent returns a typed
  routing decision, and plain code does the sending.
- **AWS Lambda, Amazon DynamoDB, Amazon SNS, Amazon SQS, Amazon CloudWatch**:
  - one Lambda handles all three inbound topics
  - DynamoDB keeps conversation memory
  - every inbound message is SNS-signature-verified
  - a dead-letter queue and alarms catch failures
- **AWS SAM** for the whole stack, plus a script that wires the event
  destinations through the AWS APIs.

Architecture diagram: [ARCHITECTURE.md](ARCHITECTURE.md).

## Challenges

- **WhatsApp's 24-hour rule.** A business can't send free text to a customer who
  hasn't written in the last 24 hours. Omnidamus tracks the window per
  conversation and switches to an approved template or a click-to-chat link.
- **Exactly-once outcomes on an at-least-once bus.** SNS can deliver twice. Inbound
  messages are de-duplicated by provider ID, and the receipt is sent at most once
  per conversation.
- **One conversation, three identities.** Phone numbers key the conversation, and
  email replies find it through a pointer from the sender's address.

## What's next

Omnidamus is the messaging layer of the AXXESS Suite (AXXESS TRIaxis, Callismatic
and Omnidamus). It will act through AXXESS's CRM, approval gate and audit log for
Triaxis Ventures' own outbound sales.

## Meta WhatsApp prize: how WhatsApp is used

WhatsApp runs through AWS End User Messaging Social:

- conversations can open with an approved template
- most of the two-way exchange happens on WhatsApp
- delivery receipts are tracked on each message
- the 24-hour customer-service window is enforced in code

## Testing instructions for judges

**TODO after deployment:** the WhatsApp number to message, the email address to
reply to, and what to say to see a full SMS → WhatsApp → email run.

## Disclosures

- Built during the submission period. No code was carried over from other
  projects.
- Uses open-source libraries (boto3, Strands Agents SDK, pydantic, cryptography,
  FastAPI).
- Built with an AI coding assistant (Claude Code).

**TODO:** the ACE opportunity ID (net-new, created on or after 14 Sep 2026, with
campaign code "AWS CDS Agentic AI Hackathon -Sept. 2026") and the demo video link.
