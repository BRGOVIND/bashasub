"""Regression tests for the API-key leak and the logging/error-handling fixes.

Every test uses a fake credential injected by conftest.py. The real key is
never read, referenced, or written by this suite.
"""

import logging
import os

import httpx
import pytest

import main
import services.translator as translator

FAKE_API_KEY = os.environ["GEMINI_API_KEY"]

SUCCESS_BODY = {"candidates": [{"content": {"parts": [{"text": "ഹലോ"}]}}]}


# ---------------------------------------------------------------- Test 1
# An upstream API failure must not return the API key.

@pytest.mark.parametrize("upstream_status", [400, 401, 403, 500])
def test_upstream_failure_never_returns_api_key(
    client, upstream, no_retry_sleep, upstream_status
):
    upstream.script(status_code=upstream_status, json_data={"error": "upstream"})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code >= 400
    assert FAKE_API_KEY not in response.text
    assert "key=" not in response.text
    assert "x-goog-api-key" not in response.text.lower()

    # And not smuggled out through a header either.
    assert FAKE_API_KEY not in str(dict(response.headers))


def test_upstream_401_reproduces_the_original_bug_safely(client, upstream):
    """The exact production failure: Gemini replies 401.

    Previously this returned HTTP 200 with the key inside the body.
    """
    upstream.script(status_code=401, json_data={"error": {"message": "API key invalid"}})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 503
    assert response.json() == {
        "error": "Translation service temporarily unavailable",
        "request_id": response.json()["request_id"],
    }
    assert FAKE_API_KEY not in response.text
    assert "generativelanguage" not in response.text
    assert "Translation Error" not in response.text


# ---------------------------------------------------------------- Test 2
# An exception whose message embeds the key must not reach the client.

def test_exception_containing_api_key_is_not_exposed(
    client, upstream, no_retry_sleep, caplog
):
    leaky = httpx.ConnectError(
        "Client error '401 Unauthorized' for url "
        f"'https://generativelanguage.googleapis.com/v1beta/models/x:generateContent?key={FAKE_API_KEY}'"
    )
    upstream.script(raises=leaky)

    with caplog.at_level(logging.DEBUG):
        response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 503
    assert FAKE_API_KEY not in response.text

    # Nor in the server-side logs.
    assert FAKE_API_KEY not in caplog.text


def test_redact_strips_credentials_from_text():
    leaky = f"failed for url 'https://x/y?key={FAKE_API_KEY}&token=abc123'"

    cleaned = translator.redact(leaky)

    assert FAKE_API_KEY not in cleaned
    assert "abc123" not in cleaned
    assert "<redacted>" in cleaned


def test_unhandled_exception_body_is_generic(client, monkeypatch, caplog):
    async def explode(*args, **kwargs):
        raise RuntimeError(f"boom with key={FAKE_API_KEY}")

    # main imported the symbol directly, so patch it there.
    monkeypatch.setattr(main, "translate_text", explode)

    with caplog.at_level(logging.ERROR):
        response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 500
    assert response.json()["error"] == "Internal server error"
    assert FAKE_API_KEY not in response.text
    assert "RuntimeError" not in response.text
    # Logged for diagnosis, but with the credential removed.
    assert FAKE_API_KEY not in caplog.text
    assert "unhandled_error" in caplog.text


# ---------------------------------------------------------------- Test 3
# The upstream request URL must not carry the key; the header must.

def test_upstream_url_contains_no_credentials(client, upstream):
    upstream.script_success()

    client.post("/translate", json={"text": "Hello"})

    assert len(upstream.calls) == 1
    url = upstream.calls[0]["url"]

    assert FAKE_API_KEY not in url
    assert "key=" not in url
    assert "?" not in url
    assert url.endswith(":generateContent")


def test_key_is_sent_in_the_x_goog_api_key_header(client, upstream):
    upstream.script_success()

    client.post("/translate", json={"text": "Hello"})

    headers = upstream.calls[0]["headers"]

    assert headers["x-goog-api-key"] == FAKE_API_KEY
    assert "Authorization" not in headers


def test_generate_content_url_helper_is_credential_free():
    url = translator.generate_content_url()

    assert FAKE_API_KEY not in url
    assert "key=" not in url


# ---------------------------------------------------------------- Test 4
# Normal logs must not contain the user's full input.

def test_normal_logs_do_not_contain_user_input(client, upstream, caplog):
    secret_text = "MEET ME AT MIDNIGHT BEHIND THE CLOCKTOWER"
    upstream.script_success()

    with caplog.at_level(logging.DEBUG):
        response = client.post("/translate", json={"text": secret_text})

    assert response.status_code == 200
    assert secret_text not in caplog.text
    assert "MIDNIGHT" not in caplog.text

    # Operational metadata is still present.
    assert "translation_request_received" in caplog.text
    assert f"character_count={len(secret_text)}" in caplog.text
    assert "target_language=ml" in caplog.text
    assert "request_id=" in caplog.text


def test_content_logging_needs_both_the_flag_and_debug_level(
    client, upstream, caplog, monkeypatch
):
    """Content logging is gated twice: the setting AND a DEBUG-level logger.

    Turning on the flag alone is not enough, which makes accidental exposure
    in production meaningfully harder.
    """
    secret_text = "OPT IN CONTENT SAMPLE"
    upstream.script_success()
    monkeypatch.setattr(translator.settings, "log_request_content", True)

    # Gate 1 only: flag on, logger still at INFO -> content stays out of logs.
    with caplog.at_level(logging.DEBUG):
        client.post("/translate", json={"text": secret_text})
    assert secret_text not in caplog.text

    caplog.clear()

    # Both gates open -> content is logged, as an operator explicitly asked.
    monkeypatch.setattr(translator.logger, "level", logging.DEBUG)
    with caplog.at_level(logging.DEBUG):
        client.post("/translate", json={"text": secret_text})

    assert secret_text in caplog.text
    # ...and even then, never the credential.
    assert FAKE_API_KEY not in caplog.text


def test_content_logging_is_off_by_default():
    assert translator.settings.log_request_content is False


# ---------------------------------------------------------------- Test 5
# Errors return appropriate non-2xx status codes.

def test_empty_text_is_a_client_error(client):
    response = client.post("/translate", json={"text": ""})

    assert response.status_code == 400
    assert response.json()["error"].startswith("Invalid request")


def test_oversized_text_is_a_client_error(client):
    oversized = "a" * (translator.settings.max_text_chars + 1)

    response = client.post("/translate", json={"text": oversized})

    assert response.status_code == 400


def test_missing_field_is_a_client_error(client):
    response = client.post("/translate", json={})

    assert response.status_code == 400


@pytest.mark.parametrize(
    "upstream_status,expected",
    [(429, 503), (500, 503), (503, 503), (401, 503), (400, 503)],
)
def test_upstream_failures_map_to_503(
    client, upstream, no_retry_sleep, upstream_status, expected
):
    upstream.script(status_code=upstream_status, json_data={})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == expected


def test_no_failure_is_ever_reported_as_200(client, upstream, no_retry_sleep):
    """The original bug returned every failure as HTTP 200."""
    for status in (401, 403, 429, 500, 502):
        upstream.script(status_code=status, json_data={})

        response = client.post("/translate", json={"text": "Hello"})

        assert response.status_code != 200, f"upstream {status} leaked through as 200"
        assert "translation" not in response.json()


def test_blocked_generation_is_not_reported_as_success(client, upstream, caplog):
    """A safety block returns 200 with no candidates. It must not become a 'translation'."""
    upstream.script(status_code=200, json_data={"promptFeedback": {"blockReason": "SAFETY"}})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 503
    assert "translation" not in response.json()


def test_every_error_response_carries_a_request_id(client, upstream, no_retry_sleep):
    upstream.script(status_code=500, json_data={})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.json()["request_id"]
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


# ---------------------------------------------------------------- Test 6
# Successful translation still works.

def test_successful_translation_still_works(client, upstream):
    upstream.script_success(text="ഹലോ, സുഖമാണോ?")

    response = client.post("/translate", json={"text": "Hello, how are you?"})

    assert response.status_code == 200
    assert response.json()["translation"] == "ഹലോ, സുഖമാണോ?"
    # Response shape preserved for the existing frontend.
    assert set(response.json()) == {"translation"}


def test_success_response_contains_no_credentials(client, upstream):
    upstream.script_success()

    response = client.post("/translate", json={"text": "Hello"})

    assert FAKE_API_KEY not in response.text
    assert FAKE_API_KEY not in str(dict(response.headers))


def test_prompt_is_still_sent_to_the_provider(client, upstream):
    upstream.script_success()

    client.post("/translate", json={"text": "Hello"})

    sent = upstream.calls[0]["body"]
    assert "Malayalam" in sent["contents"][0]["parts"][0]["text"]
    assert "Hello" in sent["contents"][0]["parts"][0]["text"]


def test_homepage_still_renders(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "BhashaSub" in response.text


# ---------------------------------------------------------------- Retry
# The tenacity decorator was previously unreachable dead code.

def test_transient_upstream_errors_are_actually_retried(client, upstream, no_retry_sleep):
    upstream.script(status_code=429, json_data={})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 503
    assert len(upstream.calls) == translator.settings.max_retries


def test_auth_failures_are_not_retried(client, upstream, no_retry_sleep):
    """401 is a misconfiguration, not a transient fault. Retrying wastes time."""
    upstream.script(status_code=401, json_data={})

    response = client.post("/translate", json={"text": "Hello"})

    assert response.status_code == 503
    assert len(upstream.calls) == 1
