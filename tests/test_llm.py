from __future__ import annotations

from tiaaa.llm import detect_provider


def test_gemini_provider_uses_current_stable_flash_default(monkeypatch) -> None:
    for name in ("LLM_MODEL", "LLM_URL", "LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    provider = detect_provider()

    assert provider.model == "gemini-3.5-flash"
    assert provider.base_url.endswith("/v1beta/openai")
