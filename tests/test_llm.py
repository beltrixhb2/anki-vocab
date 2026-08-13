"""The request actually sent to the Responses API, and how replies are read."""

from __future__ import annotations

import json

import httpx2
import openai
import pytest

from anki_vocab.llm import GenerationError, generate_entry

from tests.conftest import complete


def envelope(output, status="completed", **extra):
    return {
        "id": "resp_test", "object": "response", "created_at": 1,
        "model": "gpt-5.6-luna", "status": status, "output": output,
        "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
        **extra,
    }


def message(text):
    return [{
        "id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }]


def client_for(handler):
    return openai.OpenAI(http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))


@pytest.fixture
def capture(cfg, noun):
    """Records the request, replies with a valid entry."""
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=envelope(message(json.dumps(noun))))

    seen["client"] = client_for(handler)
    return seen


def test_it_calls_the_responses_endpoint(capture, cfg):
    generate_entry(capture["client"], cfg, "la planta")
    assert capture["url"].endswith("/v1/responses")


def test_it_uses_structured_outputs_not_function_calling(capture, cfg):
    generate_entry(capture["client"], cfg, "la planta")
    body = capture["body"]
    assert "tools" not in body and "tool_choice" not in body
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["schema"]["required"] == list(fmt["schema"]["properties"])


def test_prompt_carries_translation_and_context(capture, cfg):
    generate_entry(capture["client"], cfg, "la vela", "la bougie", "objeto que da luz")
    system, user = capture["body"]["input"]
    assert system["role"] == "system" and "lexicographer" in system["content"]
    assert "la bougie" in user["content"]
    assert "objeto que da luz" in user["content"]


def test_optional_model_settings_are_omitted_unless_configured(capture, cfg):
    generate_entry(capture["client"], cfg, "la planta")
    assert "reasoning" not in capture["body"]
    assert "max_output_tokens" not in capture["body"]

    cfg.raw["llm"].update(reasoning_effort="none", max_output_tokens=4096)
    generate_entry(capture["client"], cfg, "la planta")
    assert capture["body"]["reasoning"] == {"effort": "none"}
    assert capture["body"]["max_output_tokens"] == 4096


def test_non_strict_mode_sends_the_plain_schema(capture, cfg):
    cfg.raw["llm"]["strict_schema"] = False
    generate_entry(capture["client"], cfg, "la planta")
    fmt = capture["body"]["text"]["format"]
    assert fmt["strict"] is False
    assert len(fmt["schema"]["required"]) < len(fmt["schema"]["properties"])


def test_nulls_are_pruned_so_both_modes_agree(cfg, noun):
    """Absent and null must end up the same in both modes."""
    entry = generate_entry(
        client_for(lambda r: httpx2.Response(200, json=envelope(message(json.dumps(noun))))),
        cfg, "la planta",
    )
    assert "conjugation" not in entry
    assert entry["forms"] == {"pluriel": "les étages"}

    cfg.raw["llm"]["strict_schema"] = False
    same = generate_entry(
        client_for(lambda r: httpx2.Response(200, json=envelope(message(json.dumps(noun))))),
        cfg, "la planta",
    )
    assert same == entry


@pytest.mark.parametrize(
    "response, message_fragment",
    [
        (envelope([{
            "id": "m", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "refusal", "refusal": "No puedo."}]}]), "refused"),
        (envelope(message('{"term_source": "trunc'), status="incomplete",
                  incomplete_details={"reason": "max_output_tokens"}), "incomplete"),
        (envelope(message("not json")), "not valid JSON"),
        (envelope(message("")), "empty response"),
        (envelope(message('{"term_source": "x"}')), "does not match the schema"),
    ],
)
def test_bad_replies_produce_readable_errors(cfg, response, message_fragment):
    with pytest.raises(GenerationError, match=message_fragment):
        generate_entry(client_for(lambda r: httpx2.Response(200, json=response)), cfg, "x")


@pytest.mark.parametrize(
    "status, fragment",
    [(429, "rate limit"), (400, "status 400"), (500, "status 500")],
)
def test_api_errors_are_wrapped(cfg, status, fragment):
    def handler(request):
        return httpx2.Response(status, json={"error": {"message": "boom", "type": "x"}})

    with pytest.raises(GenerationError, match=fragment):
        generate_entry(client_for(handler), cfg, "x")


def test_a_german_entry_validates_against_its_own_preset(cfg_de):
    entry = complete(
        {
            "term_source": "el perro", "term_target": "der Hund", "ipa": "/hʊnt/",
            "pos": "Substantiv", "gender": "der", "definition": "Ein Haustier.",
            "example_target": "Der Hund bellt.", "example_source": "El perro ladra.",
            "tags": ["tema::animales"],
        },
        cfg_de.json_schema(strict=True),
    )
    result = generate_entry(
        client_for(lambda r: httpx2.Response(200, json=envelope(message(json.dumps(entry))))),
        cfg_de, "el perro",
    )
    assert result["term_target"] == "der Hund"
