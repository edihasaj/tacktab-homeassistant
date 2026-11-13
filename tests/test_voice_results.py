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
