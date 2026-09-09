import os
import sys
from pathlib import Path

# A deliberately fake credential. Assigned (not defaulted) so that a real
# GEMINI_API_KEY in the developer's environment or .env file can never be
# picked up by a test run. This must happen before config.Settings() is
# constructed at import time.
FAKE_API_KEY = "test-fake-gemini-key-DO-NOT-USE-0123456789"
os.environ["GEMINI_API_KEY"] = FAKE_API_KEY

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

# Nothing from the web application is imported at module level. The core is
# specified to work without FastAPI, httpx or pydantic, so `pytest tests/core`
# has to run in an install that has none of them. Importing the app here would
# silently break that guarantee at collection time.


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from main import app

    # raise_server_exceptions=False so the registered 500 handler is exercised
    # the same way it would be in production.
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def upstream(monkeypatch):
    """Intercept the outbound Gemini call.

    Records what was actually sent so tests can assert on the real URL and
    headers, and returns a scripted response instead of calling Google.
    """
    import httpx

    class Upstream:
        def __init__(self):
            self.calls = []

        def script(self, status_code=200, json_data=None, raises=None):
            calls = self.calls

            async def fake_post(self, url, headers=None, json=None, **kwargs):
                calls.append(
                    {
                        "url": str(url),
                        "headers": dict(headers or {}),
                        "body": json,
                    }
                )

                if raises is not None:
                    raise raises

                return httpx.Response(
                    status_code=status_code,
                    json=json_data if json_data is not None else {},
                    request=httpx.Request("POST", str(url)),
                )

            monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

        def script_success(self, text="ഹലോ"):
            self.script(
                status_code=200,
                json_data={"candidates": [{"content": {"parts": [{"text": text}]}}]},
            )

    return Upstream()


@pytest.fixture
def no_retry_sleep(monkeypatch):
    """Collapse tenacity's exponential backoff so retry paths run instantly."""
    import services.translator as translator

    async def instant(_seconds):
        return None

    monkeypatch.setattr(translator.translate_text.retry, "sleep", instant)
