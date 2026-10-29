# Omnidamus

An omnichannel conversational relay agent, built for the **AWS Communication
Developer Services (CDS) Agentic AI Partner Hackathon**
([aws-cds-partner.devpost.com](https://aws-cds-partner.devpost.com), deadline Oct 29, 2026).

In the Triaxis stack, Omnidamus does **SMS, WhatsApp and email outreach and customer service**.
Every completed conversation is published to [STILL](https://github.com/axxess-triaxis/STILL),
which turns it into a follow-up task in [AXXESS TRIaxis](https://github.com/axxess-triaxis/AXXESSTRIAXIS).
See [Part of the Triaxis stack](#part-of-the-triaxis-stack).

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

**Built and covered by tests (89 passing, no AWS account needed to run them; CI runs
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
  - WhatsApp delivery receipts are parsed and logged (matching them to stored
    messages does not work yet; see below)
  - the closing receipt goes out by SES once per completed exchange, with Reply-To
    set so the customer can answer by email
  - a completed exchange is published to STILL, the Triaxis cross-product event bus,
    as `triaxis.omnidamus` / `conversation.completed.v1`. It is published after the
    save, at most once per closing message, and never blocks the reply. STILL turns it
    into an AXXESS follow-up task. It is off unless the `StillEventBusArn` parameter is
    set. **Switched on in the deployed stack on 2026-10-29** (stack updated 12:31 UTC with
    `StillEventBusArn` pointing at the `still` bus, checked read-only with
    `aws cloudformation describe-stacks`); no completed conversation has been checked end to
    end into AXXESS yet
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

**Verified live** (27-28 Oct 2026, ap-southeast-2, Amazon Nova Pro; evidence with
message IDs and logs in [docs/LIVE_VERIFICATION.md](docs/LIVE_VERIFICATION.md)):
the stack is deployed; a conversation opened on WhatsApp with a Meta-approved template;
the agent chatted two-way on WhatsApp; the conversation closed and the SES receipt
arrived in the customer's inbox; STOP and START changed the opt-out flag.

**Not yet done** (said plainly rather than implied otherwise):

- **WhatsApp delivery is currently blocked.** Since 27 Oct 21:58, Meta has rejected
  every outbound message with error 131037: the test number's display name needs
  approval. The STOP and START confirmation messages have never reached a phone.
- **Email replies from the customer.** SES receiving is not set up: the domain's DNS
  host would not take the MX record, so the stack runs without `InboundEmailAddress`.
- **SMS/RCS.** No number; Indian SMS needs DLT registration.
- **Claude on Bedrock.** The template's default model could not be enabled (AWS
  Marketplace payment error), so the live stack uses Amazon Nova Pro.
- **Known defects found live:** delivery receipts never match a stored message; the
  receipt's subject is the agent's internal summary; a question after a closed
  conversation can be answered by email only; START while already subscribed goes to
  the agent and can send another receipt.
- **No booking system.** "Confirmed" means the time is recorded in the conversation
  and the receipt; no calendar entry is created.
- The agent's prompt has not been tuned against real conversations. The demo video
  and the Devpost submission are not done. Rich RCS content and inbound media are not
  built.

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

## Part of the Triaxis stack

[Triaxis Ventures Private Limited](https://github.com/axxess-triaxis) sells one product,
**AXXESS TRIaxis**. The other products below run the company itself. Triaxis Ventures is
Tenant 0, the dogfooded tenant, on every one of them, AXXESS included. **STILL** joins them
into one stack.

| Product | Its job in the stack | State, checked 2026-10-29 |
|---|---|---|
| [AXXESS TRIaxis](https://github.com/axxess-triaxis/AXXESSTRIAXIS) | The product we sell: an AI operating platform with governance and a human in the loop | Product: [investor.triaxisventures.com](https://investor.triaxisventures.com) (demo); [Live Beta](https://landing.triaxisventures.com) (sign-in) |
| [PORT4LLEO](https://github.com/axxess-triaxis/port4lleo) | Governance, security checks, builder analysis, product improvements | Live at [port4leo-sigma.vercel.app](https://port4leo-sigma.vercel.app) |
| [Callismatic](https://github.com/axxess-triaxis/callismatic) | Sales and outreach: triages inbound calls and messages, researches leads, places callbacks | Live on AWS Lambda (us-east-1); STILL publishing switched on 2026-10-29, end-to-end check pending |
| [Omnidamus](https://github.com/axxess-triaxis/Omnidamus) | SMS, WhatsApp and email outreach and customer service | Deployed on AWS (ap-southeast-2); STILL publishing switched on 2026-10-29, end-to-end check pending; WhatsApp sending blocked on Meta's display-name review |
| [STILL](https://github.com/axxess-triaxis/STILL) | The event bus that joins them: Callismatic leads and Omnidamus completed conversations become AXXESS tasks | Deployed (EventBridge, ap-southeast-2); STILL to AXXESS verified live 2026-10-29 |

The founder's aim is a roughly 80% automated company with the founder as the human in the
loop: AXXESS building, selling, serving customers, testing and doing outreach on itself through
these products, as a repeatable, very low-cost prototype of an AI-native startup. The founder
also states that every product except AXXESS TRIaxis was built at zero incremental cost.
*Both are founder-stated, source artifact needed.* The known running costs are small and paid
from AWS credits: STILL is estimated at under USD 2 a month, Omnidamus keeps a Route 53 zone at
USD 0.50 a month, and Amazon Bedrock tokens are billed per use.
