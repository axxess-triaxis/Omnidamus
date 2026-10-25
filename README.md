# Omnidamus

An omnichannel conversational relay agent, built for the **AWS Communication
Developer Services (CDS) Agentic AI Partner Hackathon**
([aws-cds-partner.devpost.com](https://aws-cds-partner.devpost.com), deadline Oct 29, 2026).

## Concept

Customer conversations rarely stay in one channel. Omnidamus is an agent that lets a
conversation **start in one channel and complete an action in another** — the exact
pattern the hackathon brief calls out: a notification goes out over SMS/RCS, the
two-way back-and-forth happens over WhatsApp, and a written record of the outcome
lands by email — all as one continuous conversation the agent tracks, not three
disconnected integrations.

**Reference scenario** (used to drive the initial build): an appointment/order
lifecycle notification.

1. **AWS End User Messaging (SMS/RCS)** sends the initial notification (e.g. "your
   pickup is ready — reply to schedule a time").
2. **AWS End User Messaging Social (WhatsApp)** carries the actual back-and-forth —
   the agent asks clarifying questions and reasons about the reply.
3. **Amazon SES** sends the final written confirmation/receipt once the exchange
   resolves.

The agent (not a fixed script) decides, per inbound message, whether to reply on the
same channel, hand off to a different one, or close out the conversation.

## Status

Built fresh for this hackathon (no code shared with any other project).

**Built and covered by tests (80 passing, no AWS account needed to run them; CI runs
them and `cfn-lint` on every PR):**

- **Three two-way channels in one conversation.** SMS/RCS, WhatsApp and email all
  arrive on SNS and land in the same conversation: one DynamoDB item per customer
  phone number ([`state.py`](src/omnidamus/state.py)), with inbound email matched by
  sender address. Optimistic versioning guards concurrent writes; items expire after
  30 days.
- **Authenticated intake.** Every inbound record passes SNS signature verification
  ([`sns_verify.py`](src/omnidamus/sns_verify.py)): topic allowlist, HTTPS-only SNS
  certificate URLs, SignatureVersion 1 and 2. Payloads are parsed per AWS's
  documented EUM Social, EUM SMS and SES receiving formats
  ([`inbound.py`](src/omnidamus/inbound.py)); spam/virus-flagged email is dropped.
- **The conversation loop** ([`service.py`](src/omnidamus/service.py)):
  - the agent sees the conversation's history
  - replies go to the right address for each channel
  - WhatsApp's 24-hour rule is enforced: outside the window, the customer gets an
    approved template or a click-to-chat link
  - conversations can open on WhatsApp with an approved template
  - STOP/UNSUBSCRIBE on any channel ends all contact, and START opts back in
  - SNS redeliveries are dropped
  - WhatsApp delivery receipts are recorded
  - the closing receipt goes out by SES exactly once, with Reply-To set so the
    customer can answer by email
- **Infrastructure as code** ([`template.yaml`](template.yaml), AWS SAM):
  - DynamoDB, plus SNS topics for WhatsApp, SMS and email, each with a
    service-scoped publish policy
  - an SES receipt rule (created only if `InboundEmailAddress` is set)
  - the Lambda function with least-privilege IAM
  - a dead-letter queue, and CloudWatch alarms on failures and errors
- **Post-deploy wiring** ([`scripts/connect_inbound.py`](scripts/connect_inbound.py)):
  sets the WhatsApp event destination, enables two-way SMS and activates SES
  receiving through the AWS APIs. It shows the plan by default and only acts with
  `--apply`.

**Not yet done** — say so plainly rather than implying otherwise: no AWS resources
have been provisioned for this project yet (no linked WhatsApp Business Account, no
SMS number, no verified SES sender), the stack has not been deployed, no live
message has been sent or received, and the agent's prompt has not been tuned
against real conversations. The demo video and the Devpost submission are not done.
Rich RCS content and inbound media are not built.

## AWS CDS services used

Each of these is called at runtime by the code in this repo:

| Channel | Service | boto3 client | Operations called |
|---|---|---|---|
| WhatsApp | AWS End User Messaging Social | `socialmessaging` | `send_whatsapp_message` (text and templates); `put_whatsapp_business_account_event_destinations` (wiring script) |
| SMS / RCS | AWS End User Messaging | `pinpoint-sms-voice-v2` | `send_text_message`; `update_phone_number` (two-way, wiring script) |
| Email | Amazon SES | `sesv2`, `ses` | `send_email` (receipts and email replies); receipt rule + `set_active_receipt_rule_set` (receiving) |

Amazon Bedrock (via the [Strands Agents SDK](https://strandsagents.com/)) provides
the agentic reasoning layer, per the hackathon FAQ's confirmation that Strands is an
accepted alternative to Bedrock AgentCore. Amazon DynamoDB, Amazon SNS, AWS Lambda,
Amazon SQS and Amazon CloudWatch carry the rest. See
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Setup

```bash
pip install -e ".[dev]"
cp .env.example .env  # fill in AWS + channel identifiers
pytest
```

AWS credentials resolve via the normal boto3 chain (`aws configure` / env vars) —
none are stored in this repo.

## Deploy

One-time provisioning in the AWS console (same region as the stack):

1. **AWS End User Messaging Social:** link a WhatsApp Business Account and phone
   number (use a test number, not a production one), and get your message templates
   approved by Meta.
2. **AWS End User Messaging SMS** (optional): a phone number that supports two-way
   SMS in the customer's country.
3. **Amazon SES:** verify the sender identity. In the SES sandbox, also verify each
   recipient address. For email replies, point the receiving domain's MX record at
   SES inbound for the region.
4. **Amazon Bedrock:** make sure the model in `BedrockModelId` is enabled.

Build and deploy (needs Docker and the
[AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html)):

```bash
sam build
sam deploy --guided   # set InboundEmailAddress, WhatsAppHandoffTemplate, AlarmEmail as needed
```

Connect the inbound side (dry run first, then `--apply`):

```bash
python scripts/connect_inbound.py --stack omnidamus --sms-phone-number-id <PhoneNumberId>
python scripts/connect_inbound.py --stack omnidamus --sms-phone-number-id <PhoneNumberId> --apply
```

Start a conversation, WhatsApp-first:

```bash
python scripts/start_conversation.py --function <FunctionName> --phone +919800000000 \
  --channel whatsapp --template demo_request --param Asha --context "Inbound lead"
```

or SMS-first:

```bash
python scripts/start_conversation.py --function <FunctionName> --phone +14255550123 \
  --email you@example.com --context "Order 1042 ready for pickup" \
  --notification "Your order 1042 is ready. Reply with a pickup time."
```

## License

MIT — see [LICENSE](LICENSE).
