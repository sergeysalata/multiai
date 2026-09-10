from .base import BaseProvider, ProviderError
from .vendors import (
    AnthropicProvider,
    CustomProvider,
    DeepSeekProvider,
    GeminiProvider,
    OpenAIProvider,
)

REGISTRY = {
    p.key: p
    for p in (
        AnthropicProvider,
        OpenAIProvider,
        GeminiProvider,
        DeepSeekProvider,
        CustomProvider,
    )
}

# Shown in the "add agent" form. Free text is allowed too — model names move
# faster than this file does.
SUGGESTED_MODELS = {
    "anthropic": [
        "claude-sonnet-4-5",
        "claude-opus-4-1",
        "claude-haiku-4-5",
    ],
    "openai": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
    "gemini": ["gemini-2.0-flash", "gemini-2.0-pro", "gemini-1.5-pro"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "custom": [],
}

PROVIDER_CHOICES = [
    ("anthropic", "Claude"),
    ("openai", "ChatGPT"),
    ("gemini", "Gemini"),
    ("deepseek", "DeepSeek"),
    ("custom", "Custom model"),
]


def get_provider(provider_key, api_key, model, base_url="", timeout=120) -> BaseProvider:
    cls = REGISTRY.get(provider_key)
    if cls is None:
        raise ProviderError(f"Unknown provider '{provider_key}'.")
    return cls(api_key=api_key, model=model, base_url=base_url, timeout=timeout)


def provider_label(provider_key):
    cls = REGISTRY.get(provider_key)
    return cls.label if cls else provider_key


__all__ = [
    "REGISTRY",
    "SUGGESTED_MODELS",
    "PROVIDER_CHOICES",
    "ProviderError",
    "get_provider",
    "provider_label",
]
