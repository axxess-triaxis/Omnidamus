"""Open a conversation: invoke the deployed function to send the first SMS/RCS
notification. Uses your own AWS credentials (lambda:InvokeFunction), so there is no
public endpoint to protect.

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
    parser.add_argument("--notification", required=True, help="Text of the opening SMS/RCS")
    parser.add_argument("--email", help="Customer email for the closing receipt (optional; the agent can ask)")
    parser.add_argument("--context", default="", help="What the conversation is about, for the agent")
    parser.add_argument("--region")
    args = parser.parse_args()

    payload = {
        "action": "start_conversation",
        "phone": args.phone,
        "notification": args.notification,
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
