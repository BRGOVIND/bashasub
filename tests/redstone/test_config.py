"""Configuration: defaults, env overrides, and secret hygiene."""

from redstone.config import AIConfig, Limits, RedstoneConfig, load_config

FAKE_KEY = "fake-redstone-key-DO-NOT-USE-9f8e7d6c5b4a"


def test_limits_have_bounded_defaults():
    limits = Limits()

    assert limits.max_files > 0
    assert limits.max_agent_iterations > 0
    assert limits.max_project_size > limits.max_file_size


def test_limits_read_from_env(monkeypatch):
    monkeypatch.setenv("REDSTONE_MAX_FILES", "42")
    monkeypatch.setenv("REDSTONE_MAX_AGENT_ITERATIONS", "3")

    limits = Limits.from_env()

    assert limits.max_files == 42
    assert limits.max_agent_iterations == 3


def test_invalid_or_nonpositive_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("REDSTONE_MAX_FILES", "not-a-number")
    monkeypatch.setenv("REDSTONE_MAX_AGENT_ITERATIONS", "-5")

    limits = Limits.from_env()

    assert limits.max_files == Limits().max_files
    assert limits.max_agent_iterations == Limits().max_agent_iterations


def test_ai_config_reads_env(monkeypatch):
    monkeypatch.setenv("AI_PROVIDER", "OpenAI")
    monkeypatch.setenv("AI_API_KEY", FAKE_KEY)
    monkeypatch.setenv("AI_MODEL", "some-model")

    ai = AIConfig.from_env()

    assert ai.provider == "openai"
    assert ai.model == "some-model"
    assert ai.is_configured


def test_ai_config_never_reveals_the_key():
    """Reprs are printed by debuggers, test failures and exception handlers."""
    ai = AIConfig(api_key=FAKE_KEY)

    assert FAKE_KEY not in repr(ai)
    assert FAKE_KEY not in str(ai)
    assert FAKE_KEY not in f"{ai}"
    assert "<set>" in repr(ai)
    assert "<unset>" in repr(AIConfig())


def test_health_payload_never_contains_the_key():
    config = RedstoneConfig(ai=AIConfig(api_key=FAKE_KEY))

    health = config.public_health()

    assert FAKE_KEY not in str(health)
    assert health["ai"]["configured"] is True


def test_unconfigured_ai_is_reported_honestly(monkeypatch):
    monkeypatch.delenv("AI_API_KEY", raising=False)

    assert load_config().public_health()["ai"]["configured"] is False
