"""Small optional LLM client for fit scoring and application packets."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Provider:
    base_url: str
    model: str
    api_key: str


def detect_provider() -> Provider:
    model = os.environ.get("LLM_MODEL", "")
    if url := os.environ.get("LLM_URL"):
        return Provider(url.rstrip("/"), model or "local-model", os.environ.get("LLM_API_KEY", ""))
    if key := os.environ.get("GEMINI_API_KEY"):
        return Provider(
            "https://generativelanguage.googleapis.com/v1beta/openai",
            model or "gemini-3.5-flash",
            key,
        )
    if key := os.environ.get("OPENAI_API_KEY"):
        return Provider("https://api.openai.com/v1", model or "gpt-4o-mini", key)
    raise RuntimeError("No LLM configured. Set GEMINI_API_KEY, OPENAI_API_KEY, or LLM_URL.")


class LLMClient:
    def __init__(self, provider: Provider, timeout: float = 120) -> None:
        self.provider = provider
        self.client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self.client.close()

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int = 1500,
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.provider.api_key:
            headers["Authorization"] = f"Bearer {self.provider.api_key}"
        payload = {
            "model": self.provider.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        for attempt in range(4):
            try:
                response = self.client.post(
                    f"{self.provider.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
                return str(response.json()["choices"][0]["message"]["content"])
            except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                retryable = isinstance(exc, httpx.TimeoutException) or exc.response.status_code in {429, 503}
                if not retryable or attempt == 3:
                    raise
                wait = min(2 ** (attempt + 1), 20)
                log.warning("LLM request failed temporarily; retrying in %ss", wait)
                time.sleep(wait)
        raise RuntimeError("LLM request exhausted retries")

    def ask(self, prompt: str, **kwargs: object) -> str:
        return self.chat([{"role": "user", "content": prompt}], **kwargs)


def get_client() -> LLMClient:
    return LLMClient(detect_provider())
