from __future__ import annotations

from unittest.mock import patch

from omnidamus import agent


def test_agent_does_not_print_model_output():
    """Strands prints the model's text by default, which puts customer messages in CloudWatch."""
    with patch.object(agent, "get_model"), patch.object(agent, "Agent") as build:
        agent.build_agent()
    assert build.call_args.kwargs["callback_handler"] is None
