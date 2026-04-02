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
