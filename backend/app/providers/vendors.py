from .base import BaseProvider, ProviderError


# ---------------------------------------------------------------------------
# Parameter compatibility
#
# Vendors keep retiring request parameters on newer models, and there is no
# endpoint that says which ones a given model still accepts. So: send it, and
# if the model refuses, drop it and remember that for the rest of the process.
# The first turn of a debate pays one wasted call; every later turn is clean.
# ---------------------------------------------------------------------------
_REJECTED = set()


def _rejects(provider_key, model, parameter):
    return (provider_key, model, parameter) in _REJECTED


def _remember_rejection(provider_key, model, parameter):
    _REJECTED.add((provider_key, model, parameter))


def _mentions(error, *terms):
    text = str(error).lower()
    return any(term.lower() in text for term in terms)


class AnthropicProvider(BaseProvider):
    key = "anthropic"
    label = "Claude"
    default_model = "claude-sonnet-4-5"
    api_version = "2023-06-01"

    def complete(self, system, messages, temperature, max_tokens):
        payload = {
            "model": self.model,
            "system": system,
            "messages": [
                {"role": m["role"], "content": m["content"]} for m in messages
            ],
            "max_tokens": max_tokens,
        }
        # Some newer models reject the parameter outright rather than ignoring
        # it, so once a model has refused it we stop sending it.
        if not _rejects(self.key, self.model, "temperature"):
            payload["temperature"] = temperature

        try:
            data = self._post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.api_version,
                    "content-type": "application/json",
                },
                json=payload,
            )
        except ProviderError as exc:
            if not _mentions(exc, "temperature") or "temperature" not in payload:
                raise
            _remember_rejection(self.key, self.model, "temperature")
            payload.pop("temperature")
            data = self._post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.api_key,
                    "anthropic-version": self.api_version,
                    "content-type": "application/json",
                },
                json=payload,
            )
        blocks = data.get("content", []) or []
        parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        text = "\n".join(p for p in parts if p).strip()

        usage = data.get("usage") or {}
        self.last_meta = {
            "stop_reason": data.get("stop_reason"),
            "block_types": [b.get("type") for b in blocks],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        }

        if not text:
            raise ProviderError(self._explain_empty(blocks, data, max_tokens))
        return text

    def _explain_empty(self, blocks, data, max_tokens):
        """An empty reply always has a reason. Say which one."""
        stop = data.get("stop_reason")
        usage = data.get("usage") or {}
        kinds = sorted({b.get("type", "?") for b in blocks}) or ["none"]
        produced = usage.get("output_tokens")

        if stop == "max_tokens":
            return (
                f"Claude used its whole {max_tokens}-token budget without "
                f"producing an answer (it emitted: {', '.join(kinds)}). Raise "
                f"'Max tokens per turn' on this agent — reasoning models spend "
                f"tokens thinking before they write anything."
            )
        if "thinking" in kinds:
            return (
                f"Claude returned only internal reasoning and no answer "
                f"(stop_reason={stop}, {produced} output tokens). Raising "
                f"'Max tokens per turn' usually fixes this."
            )
        if stop == "refusal":
            return "Claude declined to answer that."
        return (
            f"Claude returned no text (stop_reason={stop}, blocks="
            f"{', '.join(kinds)}, output_tokens={produced})."
        )


class OpenAIProvider(BaseProvider):
    key = "openai"
    label = "ChatGPT"
    default_model = "gpt-4o"
    endpoint = "https://api.openai.com/v1/chat/completions"

    def complete(self, system, messages, temperature, max_tokens):
        payload_messages = [{"role": "system", "content": system}] + [
            {"role": m["role"], "content": m["content"]} for m in messages
        ]
        payload = {"model": self.model, "messages": payload_messages}

        # Reasoning models reject temperature and want max_completion_tokens
        # instead of max_tokens. Both are learned from the first refusal
        # rather than guessed from the model name, which ages badly.
        if not _rejects(self.key, self.model, "temperature"):
            payload["temperature"] = temperature
        if _rejects(self.key, self.model, "max_tokens"):
            payload["max_completion_tokens"] = max_tokens
        else:
            payload["max_tokens"] = max_tokens

        url = f"{self.base_url}/chat/completions" if self.base_url else self.endpoint
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        for _ in range(3):
            try:
                data = self._post(url, headers=headers, json=payload)
                break
            except ProviderError as exc:
                if _mentions(exc, "temperature") and "temperature" in payload:
                    _remember_rejection(self.key, self.model, "temperature")
                    payload.pop("temperature")
                    continue
                if _mentions(exc, "max_completion_tokens", "max_tokens") \
                        and "max_tokens" in payload:
                    _remember_rejection(self.key, self.model, "max_tokens")
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    continue
                raise
        else:
            raise ProviderError(
                f"{self.label} kept rejecting the request parameters."
            )
        try:
            choice = data["choices"][0]
            text = (choice["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.label} returned an unexpected shape.") from exc

        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        self.last_meta = {
            "stop_reason": finish,
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
        }

        if not text:
            reasoning = self.last_meta.get("reasoning_tokens")
            if finish == "length":
                return_msg = (
                    f"{self.label} used its whole {max_tokens}-token budget "
                    f"without producing an answer"
                    + (f" ({reasoning} tokens went to reasoning)" if reasoning else "")
                    + ". Raise 'Max tokens per turn' on this agent."
                )
                raise ProviderError(return_msg)
            if finish == "content_filter":
                raise ProviderError(f"{self.label} blocked that on content policy.")
            raise ProviderError(
                f"{self.label} returned no text (finish_reason={finish}, "
                f"output_tokens={usage.get('completion_tokens')})."
            )
        return text


class GeminiProvider(BaseProvider):
    key = "gemini"
    label = "Gemini"
    default_model = "gemini-2.0-flash"
    base = "https://generativelanguage.googleapis.com/v1beta/models"

    def complete(self, system, messages, temperature, max_tokens):
        contents = [
            {
                "role": "user" if m["role"] == "user" else "model",
                "parts": [{"text": m["content"]}],
            }
            for m in messages
        ]
        data = self._post(
            f"{self.base}/{self.model}:generateContent",
            headers={"Content-Type": "application/json"},
            params={"key": self.api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            },
        )
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(
                f"Gemini returned no candidates{f' (blocked: {blocked})' if blocked else ''}."
            )
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "\n".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise ProviderError("Gemini returned an empty reply.")
        return text


class CustomProvider(OpenAIProvider):
    """Any OpenAI-compatible endpoint: OpenRouter, Groq, Together, vLLM,
    Ollama (http://localhost:11434/v1), LM Studio, your own gateway."""

    key = "custom"
    label = "Custom model"
    default_model = ""
    needs_base_url = True

    def complete(self, system, messages, temperature, max_tokens):
        if not self.base_url:
            raise ProviderError("This custom agent has no base URL set.")
        return super().complete(system, messages, temperature, max_tokens)


# ---------------------------------------------------------------------------
# Live model discovery
#
# Every vendor exposes a list endpoint, but each returns a different shape and
# includes models that cannot hold a conversation. The filters below keep the
# picker to things that will actually work as a panel member.
# ---------------------------------------------------------------------------

def _anthropic_list(self):
    data = self._get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": self.api_key,
            "anthropic-version": self.api_version,
        },
        params={"limit": 100},
    )
    models = []
    for row in data.get("data", []):
        model_id = row.get("id")
        if not model_id:
            continue
        models.append({"id": model_id, "label": row.get("display_name") or model_id})
    return models


# Endpoints that take a prompt but cannot take part in a discussion.
# "embed" rather than "embedding": self-hosted names like nomic-embed-text
# would otherwise slip through and appear as pickable panel members.
_OPENAI_SKIP = (
    "embed", "rerank", "whisper", "tts", "dall-e", "moderation", "audio",
    "image", "realtime", "transcribe", "similarity", "edit",
    "davinci", "babbage", "codex", "guard",
)


def _openai_list(self):
    url = f"{self.base_url}/models" if self.base_url else "https://api.openai.com/v1/models"
    data = self._get(url, headers={"Authorization": f"Bearer {self.api_key}"})
    rows = data.get("data", data if isinstance(data, list) else [])
    models = []
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else str(row)
        if not model_id:
            continue
        lowered = model_id.lower()
        if any(term in lowered for term in _OPENAI_SKIP):
            continue
        models.append({"id": model_id, "label": model_id, "created": row.get("created", 0)
                       if isinstance(row, dict) else 0})
    # Newest first where the vendor tells us; otherwise alphabetical.
    if any(m.get("created") for m in models):
        models.sort(key=lambda m: m.get("created", 0), reverse=True)
    else:
        models.sort(key=lambda m: m["id"])
    for m in models:
        m.pop("created", None)
    return models


def _gemini_list(self):
    data = self._get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": self.api_key, "pageSize": 200},
    )
    models = []
    for row in data.get("models", []):
        # Only models that can answer a prompt; skip embedding and tuning ones.
        if "generateContent" not in (row.get("supportedGenerationMethods") or []):
            continue
        name = (row.get("name") or "").replace("models/", "")
        if not name:
            continue
        models.append({"id": name, "label": row.get("displayName") or name})
    models.sort(key=lambda m: m["id"], reverse=True)
    return models


def _custom_list(self):
    if not self.base_url:
        raise ProviderError("This custom agent has no base URL set.")
    return _openai_list(self)


AnthropicProvider.list_models = _anthropic_list
OpenAIProvider.list_models = _openai_list
GeminiProvider.list_models = _gemini_list
CustomProvider.list_models = _custom_list
