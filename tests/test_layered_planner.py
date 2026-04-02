import json
from unittest.mock import MagicMock

from gws.providers.anthropic import AnthropicPlannerClient


def test_synthesize_includes_lane_capabilities_in_prompt():
    """The planner prompt must include lane capabilities when provided."""
    client = AnthropicPlannerClient.__new__(AnthropicPlannerClient)
    client.model = "test-model"
    client.timeout = 10.0

    mock_anthropic = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({
        "title": "Build player movement",
        "goal": "Implement WASD controls",
        "repo": "studio-ystackai",
        "allowed_paths": ["src/**"],
        "forbidden_paths": [],
        "step_type": "execute",
    }))]
    mock_anthropic.messages.create.return_value = mock_response
    client.client = mock_anthropic

    client.synthesize(
        brief="Build a platformer",
        lane="coder",
        repo_heads={"studio-ystackai": "abc123"},
        envelope={},
        lane_capabilities={"coder": "Write game code.", "artist": "Create visual assets."},
        intent_context="Browser game. HTML/CSS/JS.",
        planner_guidance="Core loop first.",
    )

    call_kwargs = mock_anthropic.messages.create.call_args[1]
    system_prompt = call_kwargs["system"]
    assert "Write game code." in system_prompt
    assert "Create visual assets." in system_prompt
    assert "Browser game. HTML/CSS/JS." in system_prompt
    assert "Core loop first." in system_prompt


def test_synthesize_works_without_optional_context():
    """The planner should work when no lane_capabilities or intent_context are passed."""
    client = AnthropicPlannerClient.__new__(AnthropicPlannerClient)
    client.model = "test-model"
    client.timeout = 10.0

    mock_anthropic = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=json.dumps({
        "title": "Do thing",
        "goal": "Goal",
        "repo": "repo-a",
        "allowed_paths": ["**"],
        "forbidden_paths": [],
        "step_type": "execute",
    }))]
    mock_anthropic.messages.create.return_value = mock_response
    client.client = mock_anthropic

    result = client.synthesize(
        brief="Do something",
        lane="coder",
        repo_heads={"repo-a": "abc"},
        envelope={},
    )
    assert result.title == "Do thing"
