"""Centralised secret redaction.

Redacts *actual secret values*, not generic words. Replacing every occurrence
of "key" or "token" would corrupt legitimate data and give false confidence;
this replaces the specific credential strings it is given, so it only ever
removes real secrets.

Used as the last line of defence: even though the credential is designed never
to reach a log or an error, any string bound for a log passes through here with
the live credential value(s) so a leak through an unexpected path is scrubbed.

Standard library only.
"""

from __future__ import annotations

__all__ = ["redact", "redact_exception"]

_PLACEHOLDER = "<redacted>"
# Shorter values are too common as substrings to redact safely (they would mangle
# ordinary text); a real API key is far longer than this.
_MIN_SECRET_LENGTH = 8


def redact(text: str, *secrets: str) -> str:
    """Replace each secret value in `text` with a placeholder."""
    if not text:
        return text
    result = str(text)
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LENGTH:
            result = result.replace(secret, _PLACEHOLDER)
    return result


def redact_exception(exc: BaseException, *secrets: str) -> str:
    """A redacted, type-prefixed rendering of an exception, safe for a log.

    The exception's own message may embed a URL or header that contains the
    credential (httpx does this), so it is never logged raw.
    """
    return redact(f"{type(exc).__name__}: {exc}", *secrets)
