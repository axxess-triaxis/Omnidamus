"""Open a conversation: invoke the deployed function to send the first SMS/RCS
notification or WhatsApp template. Uses your own AWS credentials
(lambda:InvokeFunction), so there is no public endpoint to protect.

WhatsApp-first (a business can only open a WhatsApp chat with an approved template):

    python scripts/start_conversation.py --function <FunctionName> --phone +919800000000 \
        --channel whatsapp --template demo_request --param Asha \
        --context "Inbound lead from landing form"

SMS-first:

    python scripts/start_conversation.py --function <FunctionName output> \
        --phone +14255550123 --email you@example.com \
        --context "Order 1042 ready for pickup at the Indiranagar store" \
        --notification "Your order 1042 is ready for pickup. Reply with a time that suits you."
"""

from __future__ import annotations

import argparse
import json

import boto3


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--function", required=True, help="Deployed function name (stack output FunctionName)")
    parser.add_argument("--phone", required=True, help="Customer phone number, E.164")
    parser.add_argument("--channel", choices=["sms_rcs", "whatsapp"], default="sms_rcs")
    parser.add_argument("--notification", help="Text of the opening SMS/RCS (sms_rcs channel)")
    parser.add_argument("--template", help="Approved WhatsApp template name (whatsapp channel)")
    parser.add_argument("--param", action="append", default=[], help="Template body parameter, in order; repeatable")
    parser.add_argument("--email", help="Customer email for the closing receipt (optional; the agent can ask)")
    parser.add_argument("--context", default="", help="What the conversation is about, for the agent")
    parser.add_argument("--region")
    args = parser.parse_args()

    if args.channel == "whatsapp" and not args.template:
        parser.error("--channel whatsapp needs --template")
    if args.channel == "sms_rcs" and not args.notification:
        parser.error("--channel sms_rcs needs --notification")

    payload = {
        "action": "start_conversation",
        "phone": args.phone,
        "channel": args.channel,
        "notification": args.notification,
        "template": args.template,
        "template_params": args.param,
        "email": args.email,
        "context": args.context,
    }
    response = boto3.client("lambda", region_name=args.region).invoke(
        FunctionName=args.function, Payload=json.dumps(payload).encode()
    )
    body = response["Payload"].read().decode()
    if response.get("FunctionError"):
        raise SystemExit(f"Function error: {body}")
    print(body)


if __name__ == "__main__":
    main()
