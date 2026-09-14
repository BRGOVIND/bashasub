"""Preview identity and addressing.

A preview is addressed by its OWN browser origin:

    {scheme}://{preview_id}.{preview_domain}[:{port}]/

never by a path under a shared origin. Generated JavaScript runs in that
origin, so two previews -- or a preview and Redstone itself -- sharing an
origin would share cookies, storage and same-origin fetch. The id is a
128-bit random DNS label, so it is also the capability that grants access:
unguessable, never reused, and meaningless once destroyed.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

__all__ = ["PreviewEndpoint", "new_preview_id", "is_preview_id", "PREVIEW_ID_PATTERN"]

PREVIEW_ID_PATTERN = r"pv[0-9a-f]{32}"
_PREVIEW_ID = re.compile(rf"^{PREVIEW_ID_PATTERN}$")


def new_preview_id() -> str:
    return "pv" + secrets.token_hex(16)


def is_preview_id(value: str) -> bool:
    return isinstance(value, str) and bool(_PREVIEW_ID.match(value))


@dataclass(frozen=True, slots=True)
class PreviewEndpoint:
    """Where a browser reaches one preview. Deployment decides the domain;
    nothing here hardcodes a production host."""

    preview_id: str
    scheme: str
    domain: str
    port: int = 0     # 0 = the scheme's default port (omitted from the URL)

    @property
    def host(self) -> str:
        return f"{self.preview_id}.{self.domain}"

    @property
    def origin(self) -> str:
        default = {"http": 80, "https": 443}.get(self.scheme)
        suffix = "" if self.port in (0, default) else f":{self.port}"
        return f"{self.scheme}://{self.host}{suffix}"

    @property
    def url(self) -> str:
        return self.origin + "/"
