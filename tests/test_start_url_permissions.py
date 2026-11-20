"""The public /start Function URL needs two resource-policy grants.

Lambda now refuses a public (AuthType NONE) Function URL call with 403 unless the policy allows
both lambda:InvokeFunctionUrl and lambda:InvokeFunction. Production returned 403 on 2026-11-20
with only the first grant.
"""
import pathlib
import re

TEMPLATE = (pathlib.Path(__file__).resolve().parent.parent / "template.yaml").read_text(encoding="utf-8")


def _permission_blocks():
    # Split the template on two-space-indented resource names; keep the Lambda permissions.
    parts = re.split(r"^  (\w+):\n", TEMPLATE, flags=re.M)
    named = dict(zip(parts[1::2], parts[2::2]))
    return {name: body for name, body in named.items() if body.startswith("    Type: AWS::Lambda::Permission\n")}


def test_function_url_grant_is_public_and_url_only():
    grants = [b for b in _permission_blocks().values() if "Action: lambda:InvokeFunctionUrl" in b]
    assert len(grants) == 1
    assert "FunctionUrlAuthType: NONE" in grants[0]
    assert "Condition: HasStartApi" in grants[0]


def test_invoke_function_grant_exists_and_is_limited_to_url_calls():
    grants = [b for b in _permission_blocks().values()
              if "Action: lambda:InvokeFunction\n" in b and 'Principal: "*"' in b]
    assert len(grants) == 1, "public Function URL needs lambda:InvokeFunction for principal *"
    assert "InvokedViaFunctionUrl: true" in grants[0], "grant must only allow calls made through the URL"
    assert "Condition: HasStartApi" in grants[0]
