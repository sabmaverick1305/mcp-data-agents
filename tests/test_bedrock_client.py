"""Tests for bedrock_client — factory returns correct type per env var."""
import importlib
import pytest


def _reload(monkeypatch, use_bedrock: str, region: str = "us-east-1"):
    monkeypatch.setenv("USE_BEDROCK", use_bedrock)
    monkeypatch.setenv("BEDROCK_REGION", region)
    import bedrock_client
    importlib.reload(bedrock_client)
    return bedrock_client


class TestDirectMode:
    def test_make_client_returns_async_anthropic(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        bc = _reload(monkeypatch, "false")
        client = bc.make_client()
        assert "AsyncAnthropic" in type(client).__name__

    def test_default_model_is_sonnet(self, monkeypatch):
        bc = _reload(monkeypatch, "false")
        assert "claude" in bc.default_model()

    def test_backend_label_mentions_anthropic(self, monkeypatch):
        bc = _reload(monkeypatch, "false")
        assert "Anthropic" in bc.backend_label()


class TestBedrockMode:
    def test_make_client_returns_bedrock_client(self, monkeypatch):
        bc = _reload(monkeypatch, "true", region="us-east-1")
        try:
            client = bc.make_client()
            assert "Bedrock" in type(client).__name__
        except ImportError:
            pytest.skip("anthropic Bedrock extras not installed")

    def test_default_model_is_bedrock_id(self, monkeypatch):
        bc = _reload(monkeypatch, "true")
        model = bc.default_model()
        assert "anthropic." in model or "us.anthropic." in model

    def test_backend_label_mentions_bedrock(self, monkeypatch):
        bc = _reload(monkeypatch, "true")
        assert "Bedrock" in bc.backend_label()

    def test_custom_model_id_via_env(self, monkeypatch):
        custom = "anthropic.claude-3-haiku-20240307-v1:0"
        monkeypatch.setenv("BEDROCK_MODEL_ID", custom)
        bc = _reload(monkeypatch, "true")
        assert bc.default_model() == custom
