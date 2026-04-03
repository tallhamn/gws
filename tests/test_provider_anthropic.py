from gws.providers.anthropic import AnthropicPlannerClient


def test_anthropic_planner_client_rejects_non_json_text_response():
    class ContentBlock:
        def __init__(self, text):
            self.text = text

    class Message:
        def __init__(self, text):
            self.content = [ContentBlock(text)]

    try:
        AnthropicPlannerClient._parse_response(Message("not json"))
    except ValueError as exc:
        assert str(exc) == "planner response was not valid JSON"
    else:
        raise AssertionError("expected ValueError")


def test_anthropic_planner_client_uses_first_text_block_when_thinking_block_precedes_it():
    class ThinkingBlock:
        def __init__(self, thinking):
            self.thinking = thinking

    class TextBlock:
        def __init__(self, text):
            self.text = text

    class Message:
        def __init__(self, *blocks):
            self.content = list(blocks)

    message = Message(
        ThinkingBlock("reasoning"),
        TextBlock(
            '{"title":"Plan","goal":"Do the thing","repo":"repo","work_type":"code",'
            '"description":"Ship it","allowed_paths":["src/**"],"forbidden_paths":[]}'
        ),
    )

    plan = AnthropicPlannerClient._parse_response(message)

    assert plan.title == "Plan"
    assert plan.repo == "repo"


def test_anthropic_planner_client_reads_text_from_dict_block():
    class Message:
        def __init__(self, *blocks):
            self.content = list(blocks)

    message = Message(
        {"type": "thinking", "thinking": "reasoning"},
        {
            "type": "text",
            "text": (
                '{"title":"Plan","goal":"Do the thing","repo":"repo","work_type":"code",'
                '"description":"","allowed_paths":["src/**"],"forbidden_paths":[]}'
            ),
        },
    )

    plan = AnthropicPlannerClient._parse_response(message)

    assert plan.title == "Plan"
    assert plan.repo == "repo"
