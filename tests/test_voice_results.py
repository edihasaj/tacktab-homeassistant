import json
from pathlib import Path

import pytest

from custom_components.tacktab.voice_results import result_from_intent, sanitize_media_url, tool_use_result


def test_weather_intent_is_a_bounded_native_card() -> None:
    result = result_from_intent({"response": {"speech": {"plain": {"speech": "Sunny today."}}, "data": {"result_type": "weather", "condition": "sunny", "temperature": 22, "high": 24, "low": 12, "forecast": "Clear this evening"}}})
    assert result == {"version": 1, "kind": "weather", "title": "Weather", "text": "Sunny today.", "condition": "sunny", "temperature": 22, "high": 24, "low": 12, "forecast": "Clear this evening"}


def test_media_urls_allow_https_or_relative_authenticated_paths_only() -> None:
    assert sanitize_media_url("https://cdn.example.test/image.jpg") == "https://cdn.example.test/image.jpg"
    assert sanitize_media_url("/api/image_proxy/media") == "/api/image_proxy/media"
    assert sanitize_media_url("file:///private/image.jpg") is None
    assert sanitize_media_url("http://192.168.1.4/image.jpg") is None
    assert sanitize_media_url("//attacker.test/image.jpg") is None


def test_tool_line_prefers_the_safe_ha_outcome() -> None:
    assert tool_use_result("intent__goodnight", {"message": "Ran script Goodnight"}, False)["text"] == "Ran script Goodnight"


_VECTORS = json.loads((Path(__file__).parent / "fixtures" / "voice-url-vectors.json").read_text())


@pytest.mark.parametrize("vector", _VECTORS["vectors"], ids=lambda vector: repr(vector["input"])[:48])
def test_shared_url_vectors_match_the_android_and_ios_policy(vector: dict) -> None:
    # tests/fixtures/voice-url-vectors.json is a copy of tacktab/shared/voice-url-vectors.json.
    assert sanitize_media_url(vector["input"]) == vector["expected"]


def test_url_length_is_bounded_not_truncated() -> None:
    limit = _VECTORS["maxLength"]
    exact = "https://x.test/" + "a" * (limit - len("https://x.test/"))
    assert sanitize_media_url(exact) == exact
    assert sanitize_media_url(exact + "a") is None
    assert sanitize_media_url("/" + "a" * limit) is None


def test_malformed_url_falls_back_to_the_spoken_text_card() -> None:
    result = result_from_intent({"response": {"speech": {"plain": {"speech": "Here is the picture."}}, "data": {"result_type": "image", "url": "https://["}}})
    assert result == {"version": 1, "kind": "text", "title": "Assistant", "text": "Here is the picture."}


def test_zero_degrees_is_kept() -> None:
    celsius = result_from_intent({"response": {"speech": {"plain": {"speech": "Freezing."}}, "data": {"result_type": "weather", "temperature": 0}}})
    assert celsius is not None and celsius["temperature"] == 0
    fallback = result_from_intent({"response": {"speech": {"plain": {"speech": "Freezing."}}, "data": {"result_type": "weather", "temp": 0, "high": 0, "low": -3}}})
    assert fallback is not None and fallback["temperature"] == 0 and fallback["high"] == 0 and fallback["low"] == -3


def test_media_card_uses_the_normalized_https_url() -> None:
    result = result_from_intent({"response": {"speech": {"plain": {"speech": "Look."}}, "data": {"result_type": "video", "url": "HTTPS://cdn.example.test/v.mp4"}}})
    assert result is not None and result["kind"] == "video" and result["url"] == "https://cdn.example.test/v.mp4"


def test_malformed_ipv6_host_falls_back_to_the_text_card() -> None:
    for url in ("https://[::::]/image.png", "https://[.]/x"):
        assert sanitize_media_url(url) is None
        result = result_from_intent({"response": {"speech": {"plain": {"speech": "Here."}}, "data": {"result_type": "image", "url": url}}})
        assert result == {"version": 1, "kind": "text", "title": "Assistant", "text": "Here."}
