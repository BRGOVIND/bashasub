import re

import httpx

from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_attempt
from tenacity import wait_exponential

from config import settings
from logger import get_logger

logger = get_logger("translator")

GENERATE_CONTENT_PATH = "/models/{model}:generateContent"

# Matches credential-bearing query parameters, e.g. "?key=abc" or "&token=abc".
_CREDENTIAL_QUERY = re.compile(
    r"(?i)\b(key|token|access_token|api_key|authorization)=[^&\s\"'>]+"
)


class TranslationError(Exception):
    """Base class for every translation failure."""


class TranslationClientError(TranslationError):
    """The caller sent something we cannot process. Maps to HTTP 400."""


class TranslationUnavailableError(TranslationError):
    """The upstream provider failed or is unreachable. Maps to HTTP 503."""


class RetryableUpstreamError(TranslationUnavailableError):
    """A transient upstream condition that is worth retrying."""


def redact(message: str) -> str:
    """Remove anything credential-shaped from text destined for a log.

    Defence in depth. The key is no longer placed in URLs, so it should
    never reach here, but a provider or library could still echo it back
    inside an error message.
    """
    text = str(message)

    key = settings.gemini_api_key
    if key and len(key) >= 8:
        text = text.replace(key, "<redacted>")

    return _CREDENTIAL_QUERY.sub(r"\1=<redacted>", text)


def generate_content_url() -> str:
    """Build the upstream endpoint. Carries no credentials by design."""
    base = settings.gemini_base_url.rstrip("/")
    path = GENERATE_CONTENT_PATH.format(model=settings.gemini_model)
    return f"{base}{path}"


def _auth_headers() -> dict[str, str]:
    # The key travels in a header, never in the URL, so it cannot end up in
    # provider access logs, proxy logs, or httpx exception messages.
    return {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }


def _extract_text(data: dict) -> str:
    """Pull the translated text out of a Gemini response.

    A blocked or truncated generation returns no usable candidate, so this
    is guarded rather than indexed blindly.
    """
    try:
        parts = data["candidates"][0]["content"]["parts"]
        text = parts[0]["text"]
    except (KeyError, IndexError, TypeError):
        # Deliberately does not log `data`: it echoes user content.
        logger.warning("gemini_response_unusable reason=no_candidate_text")
        raise TranslationUnavailableError("Translation service returned no usable result.")

    if not isinstance(text, str) or not text.strip():
        logger.warning("gemini_response_unusable reason=empty_text")
        raise TranslationUnavailableError("Translation service returned no usable result.")

    return text


async def _call_gemini(text: str, request_id: str) -> str:
    """Perform exactly one upstream call. Raises typed errors, never returns them."""
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": f"Translate the following text into Malayalam:\n\n{text}"}
                ]
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            response = await client.post(
                generate_content_url(),
                headers=_auth_headers(),
                json=payload,
            )
    except httpx.TimeoutException:
        logger.warning(f"gemini_timeout request_id={request_id}")
        raise RetryableUpstreamError("Translation service timed out.")
    except httpx.RequestError as exc:
        # Log the exception type only. The string form can include the URL.
        logger.warning(
            f"gemini_transport_error request_id={request_id} type={type(exc).__name__}"
        )
        raise RetryableUpstreamError("Translation service is unreachable.")

    status = response.status_code
    logger.info(f"gemini_response request_id={request_id} status={status}")

    if status in (401, 403):
        # Server-side misconfiguration. Never disclose this to the caller.
        logger.error(
            f"gemini_auth_failed request_id={request_id} status={status} "
            "action=check GEMINI_API_KEY environment variable"
        )
        raise TranslationUnavailableError("Translation service is not available.")

    if status == 429 or status >= 500:
        logger.warning(f"gemini_upstream_error request_id={request_id} status={status}")
        raise RetryableUpstreamError("Translation service is busy.")

    if status >= 400:
        logger.error(f"gemini_rejected_request request_id={request_id} status={status}")
        raise TranslationUnavailableError("Translation service rejected the request.")

    try:
        data = response.json()
    except ValueError:
        logger.error(f"gemini_bad_json request_id={request_id}")
        raise TranslationUnavailableError("Translation service returned an invalid response.")

    return _extract_text(data)


@retry(
    retry=retry_if_exception_type(RetryableUpstreamError),
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)
async def translate_text(text: str, request_id: str = "-") -> str:
    """Translate text into Malayalam.

    Returns translated text, or raises a TranslationError subclass. It never
    returns an error string: callers must be able to distinguish success from
    failure without inspecting the payload.
    """
    if not text or not text.strip():
        raise TranslationClientError("Enter some text to translate.")

    if len(text) > settings.max_text_chars:
        raise TranslationClientError(
            f"Text is too long. The limit is {settings.max_text_chars} characters."
        )

    logger.info(
        f"translation_request_received request_id={request_id} "
        f"character_count={len(text)} target_language=ml"
    )

    if settings.log_request_content:
        logger.debug(f"translation_input request_id={request_id} content={text!r}")

    result = await _call_gemini(text, request_id)

    logger.info(f"translation_succeeded request_id={request_id}")

    return result
