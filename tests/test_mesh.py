import json
from types import SimpleNamespace

from app.config import Settings
from app.services.mesh import MeshNarrativeGenerator
from app.services.narrative import decode_narrative


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self._content = content

    def create(self, **kwargs):
        message = SimpleNamespace(content=self._content)
        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=50)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _generator_with_response(content: str) -> MeshNarrativeGenerator:
    generator = MeshNarrativeGenerator(Settings(mesh_api_key="fake-key"))
    generator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(content))
    )
    return generator


def test_generate_splits_understanding_and_points() -> None:
    content = json.dumps(
        {
            "activity_understanding": "Building a real-time voice agent.",
            "recommendation_points": [
                "Cartesia Sonic wins on latency.",
                "ElevenLabs Turbo v2.5 wins on naturalness.",
            ],
            "catalog_item_ids": [4, 3],
        }
    )
    generator = _generator_with_response(content)
    result = generator.generate(
        "summary",
        [
            {"id": 4, "title": "Cartesia Sonic", "provider": "Cartesia"},
            {"id": 3, "title": "ElevenLabs Turbo v2.5", "provider": "ElevenLabs"},
        ],
    )
    parsed = decode_narrative(result.narrative)
    assert parsed.understanding == "Building a real-time voice agent."
    assert parsed.points == [
        "Cartesia Sonic wins on latency.",
        "ElevenLabs Turbo v2.5 wins on naturalness.",
    ]
    assert result.catalog_item_ids == [4, 3]


def test_generate_strips_id_mentions_from_both_sections() -> None:
    content = json.dumps(
        {
            "activity_understanding": "Comparing Cartesia Sonic (ID 4) to peers.",
            "recommendation_points": [
                "Cartesia Sonic (ID 4) beats ElevenLabs Turbo v2.5 (id: 3) on latency."
            ],
            "catalog_item_ids": [4],
        }
    )
    generator = _generator_with_response(content)
    result = generator.generate(
        "summary", [{"id": 4, "title": "Cartesia Sonic", "provider": "Cartesia"}]
    )
    parsed = decode_narrative(result.narrative)
    assert "4" not in parsed.understanding
    assert "ID" not in parsed.understanding
    assert "4" not in parsed.points[0]
    assert "3" not in parsed.points[0]
    assert "Cartesia Sonic" in parsed.points[0]
    assert "ElevenLabs Turbo v2.5" in parsed.points[0]


def test_candidate_id_is_not_embedded_in_the_readable_description() -> None:
    """The candidate_id must still reach the model (for catalog_item_ids grounding), but not as
    part of the human-readable name/description text the narrative gets built from."""
    captured = {}

    class RecordingCompletions:
        def create(self, **kwargs):
            captured["messages"] = kwargs["messages"]
            content = json.dumps(
                {
                    "activity_understanding": "ok",
                    "recommendation_points": ["ok"],
                    "catalog_item_ids": [4],
                }
            )
            message = SimpleNamespace(content=content)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    generator = MeshNarrativeGenerator(Settings(mesh_api_key="fake-key"))
    generator.client = SimpleNamespace(
        chat=SimpleNamespace(completions=RecordingCompletions())
    )
    generator.generate(
        "summary", [{"id": 4, "title": "Cartesia Sonic", "provider": "Cartesia"}]
    )
    user_message = captured["messages"][1]["content"]
    assert "- Cartesia Sonic (Cartesia, candidate_id=4)." in user_message
    assert "- 4: Cartesia Sonic" not in user_message


def test_generate_captures_latency_and_tokens() -> None:
    content = json.dumps(
        {"activity_understanding": "ok", "recommendation_points": ["ok"]}
    )
    generator = _generator_with_response(content)
    result = generator.generate("summary", [])
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 50


def test_generate_captures_model_and_raw_prompt_response() -> None:
    """P1-4 auditability: the exact model + wire messages/response must be captured
    so a Recommendation row is self-explanatory without depending on LangSmith
    tracing, which is opt-in and off by default."""
    content = json.dumps(
        {"activity_understanding": "ok", "recommendation_points": ["ok"]}
    )
    generator = _generator_with_response(content)
    generator.model = "gpt-4o-mini"
    result = generator.generate(
        "summary", [{"id": 4, "title": "Cartesia Sonic", "provider": "Cartesia"}]
    )
    assert result.model == "gpt-4o-mini"
    assert result.raw_response == content
    prompt_messages = json.loads(result.raw_prompt)
    assert prompt_messages[0]["role"] == "system"
    assert prompt_messages[1]["role"] == "user"
    assert "Cartesia Sonic" in prompt_messages[1]["content"]


def test_generate_cost_is_none_when_pricing_lookup_fails() -> None:
    """Cost is best-effort — a failed/unavailable pricing lookup must never break
    latency/token capture, the more load-bearing efficiency signals."""
    content = json.dumps(
        {"activity_understanding": "ok", "recommendation_points": ["ok"]}
    )
    generator = _generator_with_response(content)
    generator._pricing_per_1m_tokens = lambda model: None
    result = generator.generate("summary", [])
    assert result.cost_usd is None
    assert result.prompt_tokens == 100


def test_generate_computes_cost_from_pricing() -> None:
    content = json.dumps(
        {"activity_understanding": "ok", "recommendation_points": ["ok"]}
    )
    generator = _generator_with_response(content)
    generator._pricing_per_1m_tokens = lambda model: (2.0, 8.0)
    result = generator.generate("summary", [])
    # 100 prompt tokens @ $2/1M + 50 completion tokens @ $8/1M
    assert result.cost_usd == (100 / 1_000_000) * 2.0 + (50 / 1_000_000) * 8.0


def test_pricing_lookup_parses_and_caches(monkeypatch) -> None:
    calls = {"count": 0}

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return [
                {"id": "other/model", "pricing": {"prompt_usd_per_1m": "1"}},
                {
                    "id": "fake/embed-model",
                    "pricing": {
                        "prompt_usd_per_1m": "2.5",
                        "completion_usd_per_1m": "9.5",
                    },
                },
            ]

    def fake_get(url, headers=None, timeout=None):
        calls["count"] += 1
        return FakeResponse()

    import app.services.mesh as mesh_module

    monkeypatch.setattr(mesh_module.httpx, "get", fake_get)
    generator = MeshNarrativeGenerator(Settings(mesh_api_key="fake-key"))
    generator.model = "fake/embed-model"

    pricing = generator._pricing_per_1m_tokens("fake/embed-model")
    assert pricing == (2.5, 9.5)
    # Second lookup for the same model must be served from cache, not a new request.
    generator._pricing_per_1m_tokens("fake/embed-model")
    assert calls["count"] == 1


def test_pricing_lookup_returns_none_on_request_failure(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):
        raise RuntimeError("network error")

    import app.services.mesh as mesh_module

    monkeypatch.setattr(mesh_module.httpx, "get", fake_get)
    generator = MeshNarrativeGenerator(Settings(mesh_api_key="fake-key"))
    assert generator._pricing_per_1m_tokens("any/model") is None
