import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from models import TranslationRequest


def test_translation_request():

    request = TranslationRequest(
        text="Hello"
    )

    assert request.text == "Hello"