import re
import time

from ..text_cleanup import clean as clean_output
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

# Learned per model: the largest number of output tokens it will accept.
_CEILINGS = {}


def _ceiling_from(error):
    """Pull the real limit out of a "maximum allowed output tokens" error."""
    text = str(error)
    if "maximum" not in text.lower():
        return None
    numbers = [int(n) for n in re.findall(r"\b(\d{3,6})\b", text)]
    return min(numbers) if numbers else None


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

    supports_documents = True

    # Anthropic requires max_tokens. With no way to say "the model's own
    # maximum", ask for a high number and learn the real ceiling from the
    # error, which names it.
    AUTO_MAX_TOKENS = 32000

    WEB_SEARCH_TOOL = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 5,
    }

    def complete(self, system, messages, temperature, max_tokens,
                 documents=None, document_fallback="", cache_prefix="",
                 web_search=False):
        use_docs = bool(documents) and not _rejects(self.key, self.model, "documents")
        use_search = web_search and not _rejects(self.key, self.model, "web_search")
        payload = {
            "model": self.model,
            "system": system,
            "messages": self._messages(
                messages, documents if use_docs else None,
                "" if use_docs else document_fallback,
                cache_prefix,
            ),
            "max_tokens": self._budget(max_tokens),
        }
        if use_search:
            payload["tools"] = [self.WEB_SEARCH_TOOL]
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
            retry = False
            if _mentions(exc, "temperature") and "temperature" in payload:
                _remember_rejection(self.key, self.model, "temperature")
                payload.pop("temperature")
                retry = True
            elif _ceiling_from(exc) and not _CEILINGS.get((self.key, self.model)):
                # "max_tokens: 32000 > 8192, which is the maximum…" — the error
                # names the real limit, so take it and never ask for more.
                _CEILINGS[(self.key, self.model)] = _ceiling_from(exc)
                payload["max_tokens"] = self._budget(max_tokens)
                retry = True
            elif use_search and _mentions(exc, "web_search", "tool", "tools"):
                _remember_rejection(self.key, self.model, "web_search")
                payload.pop("tools", None)
                use_search = False
                retry = True
            elif use_docs and _mentions(exc, "document", "pdf", "media_type",
                                        "content block", "unsupported"):
                # This model cannot take PDFs. Fall back to the extracted text
                # so the files still reach it, and stop trying for this model.
                _remember_rejection(self.key, self.model, "documents")
                payload["messages"] = self._messages(
                    messages, None, document_fallback, cache_prefix
                )
                use_docs = False
                retry = True
            if not retry:
                raise
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
        produced = "\n".join(p for p in parts if p)
        text = clean_output(produced)

        usage = data.get("usage") or {}
        self.last_meta = {
            "stop_reason": data.get("stop_reason"),
            "block_types": [b.get("type") for b in blocks],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            # Written on the first turn, read on every turn after it.
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            # Before cleanup: a long reply that was mostly scaffolding should
            # not read as the model having little to say.
            "raw_chars": len(produced.strip()),
        }

        if not text:
            if data.get("stop_reason") == "max_tokens" and not _rejects(
                self.key, self.model, "small_budget"
            ):
                _remember_rejection(self.key, self.model, "small_budget")
                return self.complete(
                    system, messages, temperature, max_tokens,
                    documents, document_fallback, cache_prefix, web_search,
                )
            raise ProviderError(self._explain_empty(blocks, data, max_tokens))
        return text

    def _budget(self, max_tokens):
        """How many output tokens to ask for."""
        learned = _CEILINGS.get((self.key, self.model))
        if not max_tokens:
            return learned or self.AUTO_MAX_TOKENS
        if _rejects(self.key, self.model, "small_budget"):
            max_tokens = min(max(max_tokens * 4, 8000), 32000)
        return min(max_tokens, learned) if learned else max_tokens

    @staticmethod
    def _messages(messages, documents, fallback_text, cache_prefix=""):
        """Documents and the unchanging file text ride ahead of the prompt.

        The last block before the conversation carries a cache breakpoint, so
        everything above it — the PDFs and the file text — is billed once and
        then reused on later turns instead of re-read every time.
        """
        out = [{"role": m["role"], "content": m["content"]} for m in messages]
        if not out:
            return out

        blocks = []
        for d in documents or []:
            blocks.append({
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": d["media_type"],
                    "data": d["data"],
                },
                "title": d["filename"],
            })
        stable = cache_prefix or fallback_text
        if stable:
            blocks.append({"type": "text", "text": stable})

        if not blocks:
            return out

        # Mark the end of the unchanging part.
        blocks[-1] = dict(blocks[-1], cache_control={"type": "ephemeral"})
        blocks.append({"type": "text", "text": out[0]["content"]})
        out[0] = {"role": out[0]["role"], "content": blocks}
        return out

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

    supports_documents = True

    def complete(self, system, messages, temperature, max_tokens,
                 documents=None, document_fallback="", cache_prefix="",
                 web_search=False):
        # OpenAI caches long identical prefixes automatically — there is no
        # breakpoint to set, the files simply have to come first.
        use_docs = bool(documents) and not _rejects(self.key, self.model, "documents")
        use_search = web_search and not _rejects(self.key, self.model, "web_search")
        payload = {
            "model": self.model,
            "messages": self._messages(
                system, messages, documents if use_docs else None,
                ("" if use_docs else document_fallback) or cache_prefix,
            ),
        }

        # Reasoning models reject temperature and want max_completion_tokens
        # instead of max_tokens. Both are learned from the first refusal
        # rather than guessed from the model name, which ages badly.
        if not _rejects(self.key, self.model, "temperature"):
            payload["temperature"] = temperature
        if max_tokens:
            if _rejects(self.key, self.model, "small_budget"):
                # Already ran out of room once; do not make the user pay for
                # that discovery on every turn.
                max_tokens = min(max(max_tokens * 4, 8000), 32000)
            if _rejects(self.key, self.model, "max_tokens"):
                payload["max_completion_tokens"] = max_tokens
            else:
                payload["max_tokens"] = max_tokens
        # No cap set: the parameter is optional here, so leave it out and let
        # the model use its own maximum.
        if use_search:
            payload["web_search_options"] = {}

        url = f"{self.base_url}/chat/completions" if self.base_url else self.endpoint
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        attempts = []
        last_error = None
        escalated = False
        # One attempt per parameter that might be refused, plus one to succeed,
        # plus one for a reasoning model that needs a bigger budget.
        for _ in range(6):
            attempts.append(
                "+".join(
                    k for k in ("web_search_options", "temperature",
                                "max_tokens", "max_completion_tokens")
                    if k in payload
                ) or "plain"
            )
            try:
                data = self._post(url, headers=headers, json=payload)
                break
            except ProviderError as exc:
                last_error = exc
                if _mentions(exc, "temperature") and "temperature" in payload:
                    _remember_rejection(self.key, self.model, "temperature")
                    payload.pop("temperature")
                    continue
                if _mentions(exc, "max_completion_tokens", "max_tokens") \
                        and "max_tokens" in payload:
                    _remember_rejection(self.key, self.model, "max_tokens")
                    payload["max_completion_tokens"] = payload.pop("max_tokens")
                    continue
                if use_search and _mentions(exc, "web_search", "tool", "unsupported",
                                            "unrecognized", "not supported"):
                    _remember_rejection(self.key, self.model, "web_search")
                    payload.pop("web_search_options", None)
                    use_search = False
                    continue
                if use_docs and _mentions(exc, "file", "pdf", "content", "unsupported",
                                          "invalid_type"):
                    _remember_rejection(self.key, self.model, "documents")
                    payload["messages"] = self._messages(
                        system, messages, None, document_fallback or cache_prefix
                    )
                    use_docs = False
                    continue
                raise
        else:
            raise ProviderError(
                f"{last_error} {self.label} refused every combination "
                f"(tried: {'; '.join(attempts)}). If a plain request also "
                "fails, the model name is the likely cause — check it against "
                "the list your key returns in the agent form."
            )
        try:
            choice = data["choices"][0]
            produced = choice["message"]["content"] or ""
            text = clean_output(produced)
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self.label} returned an unexpected shape.") from exc

        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        self.last_meta = {
            "raw_chars": len(produced.strip()),
            "stop_reason": finish,
            "cache_read": (usage.get("prompt_tokens_details") or {}).get(
                "cached_tokens"
            ),
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"
            ),
        }

        if not text:
            reasoning = self.last_meta.get("reasoning_tokens")
            if finish == "length":
                # A reasoning model can spend the whole budget thinking. Rather
                # than failing the turn and asking the user to go and change a
                # setting, try once more with room to actually answer.
                if not escalated and not _rejects(
                    self.key, self.model, "small_budget"
                ):
                    escalated = True
                    _remember_rejection(self.key, self.model, "small_budget")
                    return self.complete(
                        system, messages, temperature, max_tokens,
                        documents, document_fallback, cache_prefix, web_search,
                    )
                raise ProviderError(
                    f"{self.label} used its whole "
                    f"{max_tokens or 'available'}-token budget "
                    f"without producing an answer"
                    + (f" ({reasoning} tokens went to reasoning)" if reasoning else "")
                    + ". Raise 'Max tokens per turn' on this agent — a "
                    "reasoning model needs room to think and then write."
                )
            if finish == "content_filter":
                raise ProviderError(f"{self.label} blocked that on content policy.")
            raise ProviderError(
                f"{self.label} returned no text (finish_reason={finish}, "
                f"output_tokens={usage.get('completion_tokens')})."
            )
        return text


def _openai_messages(system, messages, documents, fallback_text):
    """PDFs go in as file parts on the first user message."""
    out = [{"role": "system", "content": system}] + [
        {"role": m["role"], "content": m["content"]} for m in messages
    ]
    first = next((m for m in out if m["role"] == "user"), None)
    if first is None:
        return out
    if documents:
        parts = [
            {
                "type": "file",
                "file": {
                    "filename": d["filename"],
                    "file_data": f"data:{d['media_type']};base64,{d['data']}",
                },
            }
            for d in documents
        ]
        parts.append({"type": "text", "text": first["content"]})
        first["content"] = parts
    elif fallback_text:
        first["content"] = f"{fallback_text}\n\n{first['content']}"
    return out


OpenAIProvider._messages = staticmethod(_openai_messages)


class GeminiProvider(BaseProvider):
    key = "gemini"
    label = "Gemini"
    default_model = "gemini-2.0-flash"
    base = "https://generativelanguage.googleapis.com/v1beta/models"

    supports_documents = True

    def complete(self, system, messages, temperature, max_tokens,
                 documents=None, document_fallback="", cache_prefix="",
                 web_search=False):
        use_docs = bool(documents) and not _rejects(self.key, self.model, "documents")
        # Newer models take google_search; 1.5-era ones want the retrieval
        # form. Try the modern one, fall back, then give up on search.
        search_mode = ""
        if web_search and not _rejects(self.key, self.model, "web_search"):
            search_mode = (
                "google_search_retrieval"
                if _rejects(self.key, self.model, "google_search")
                else "google_search"
            )
        # Gemma models on this endpoint reject a system instruction, and answer
        # a request carrying one with a 500 rather than a useful error. Learned
        # from the first failure rather than guessed from the model name.
        use_system = not _rejects(self.key, self.model, "system")

        def build():
            contents = self._contents(
                messages, documents if use_docs else None,
                ("" if use_docs else document_fallback) or cache_prefix,
                "" if use_system else system,
            )
            body = {
                "contents": contents,
                "generationConfig": _generation_config(
                    temperature, max_tokens,
                    _rejects(self.key, self.model, "small_budget"),
                ),
            }
            if use_system:
                body["systemInstruction"] = {"parts": [{"text": system}]}
            if search_mode:
                body["tools"] = [{search_mode: {}}]
            return body

        url = f"{self.base}/{self.model}:generateContent"
        headers = {"Content-Type": "application/json"}
        params = {"key": self.api_key}

        data = None
        transient_retries = 1
        attempts = []
        for _ in range(4):
            body = build()
            attempts.append(
                "+".join(
                    k for k in ("systemInstruction", "tools") if k in body
                ) or "plain"
            )
            try:
                data = self._post(url, headers=headers, params=params, json=body)
                break
            except ProviderError as exc:
                internal = _mentions(exc, "500", "internal error", "503",
                                     "overloaded", "unavailable")
                if search_mode and (internal or _mentions(
                        exc, "google_search", "tool", "unsupported", "not supported")):
                    if search_mode == "google_search":
                        _remember_rejection(self.key, self.model, "google_search")
                        search_mode = "google_search_retrieval"
                    else:
                        _remember_rejection(self.key, self.model, "web_search")
                        search_mode = ""
                    continue
                if use_docs and (internal or _mentions(exc, "inline_data", "mime",
                                                       "unsupported", "media")):
                    _remember_rejection(self.key, self.model, "documents")
                    use_docs = False
                    continue
                if use_system and (internal or _mentions(exc, "systeminstruction",
                                                         "system_instruction")):
                    _remember_rejection(self.key, self.model, "system")
                    use_system = False
                    continue
                if internal and transient_retries:
                    # Gemini returns 500 for genuinely transient faults too.
                    transient_retries -= 1
                    time.sleep(1.5)
                    continue
                # Out of fallbacks. Say what was attempted, so the failure is
                # diagnosable rather than just "500".
                raise ProviderError(
                    f"{exc} Tried: {', '.join(attempts)}. "
                    "If this model works nowhere, check the model name against "
                    "the list your key returns — Gemma models in particular "
                    "reject system instructions and tools."
                ) from exc
        if data is None:
            raise ProviderError(
                f"Gemini failed on every attempt (tried: {', '.join(attempts)}). "
                "If a plain request also fails, the model name is the likely "
                "cause — check it against the list your key returns in the "
                "agent form."
            )
        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(
                f"Gemini returned no candidates{f' (blocked: {blocked})' if blocked else ''}."
            )
        parts = candidates[0].get("content", {}).get("parts", []) or []

        # Reasoning arrives as its own parts, flagged "thought". They are the
        # model's scratchpad — the plan, the drafts, the self-criticism — and
        # concatenating them onto the answer is what produced all the visible
        # "thinking out loud" in the transcript.
        spoken, thought = [], []
        for part in parts:
            body = part.get("text") or ""
            if not body:
                continue
            (thought if part.get("thought") else spoken).append(body)

        produced = "\n".join(spoken)
        thinking = "\n".join(thought)

        self.last_meta = dict(
            self.last_meta,
            raw_chars=len(produced.strip()),
            thought_chars=len(thinking.strip()),
            parts=len(parts),
        )

        if not produced.strip() and thinking.strip():
            # Everything it produced was reasoning: the budget ran out before
            # it wrote an answer. Give it room once rather than failing the turn.
            if not _rejects(self.key, self.model, "small_budget"):
                _remember_rejection(self.key, self.model, "small_budget")
                return self.complete(
                    system, messages, temperature, max_tokens,
                    documents, document_fallback, cache_prefix, web_search,
                )
            raise ProviderError(
                f"Gemini spent its whole {max_tokens}-token budget thinking and "
                "never wrote an answer. Raise 'Max tokens per turn' on this agent."
            )

        text = clean_output(produced)
        if not text:
            raise ProviderError("Gemini returned an empty reply.")
        return text


def _generation_config(temperature, max_tokens, escalate):
    config = {"temperature": temperature}
    if max_tokens:
        config["maxOutputTokens"] = (
            min(max(max_tokens * 4, 8000), 32000) if escalate else max_tokens
        )
    # Otherwise omitted: Gemini then uses the model's own output limit.
    return config


def _gemini_contents(messages, documents, fallback_text, inline_system=""):
    """Build the request contents.

    `inline_system` is set only when the model refused a system instruction:
    the same text is folded into the first user message instead, so the agent
    still knows its name, the room and its own instructions.
    """
    out = []
    for index, m in enumerate(messages):
        parts = []
        if index == 0 and documents:
            parts += [
                {"inline_data": {"mime_type": d["media_type"], "data": d["data"]}}
                for d in documents
            ]
        text = m["content"]
        if index == 0 and not documents and fallback_text:
            text = f"{fallback_text}\n\n{text}"
        if index == 0 and inline_system:
            text = f"{inline_system}\n\n{text}"
        parts.append({"text": text})
        out.append(
            {"role": "user" if m["role"] == "user" else "model", "parts": parts}
        )
    return out


GeminiProvider._contents = staticmethod(_gemini_contents)


class DeepSeekProvider(OpenAIProvider):
    """DeepSeek speaks the OpenAI chat-completions dialect.

    Worth knowing about deepseek-reasoner: its chain of thought comes back in
    a separate `reasoning_content` field, not in `content`. Reading only
    `content`, as the OpenAI adapter does, keeps the scratchpad out of the
    room without any extra work.
    """

    key = "deepseek"
    label = "DeepSeek"
    default_model = "deepseek-chat"
    endpoint = "https://api.deepseek.com/v1/chat/completions"
    api_base = "https://api.deepseek.com/v1"
    needs_base_url = False
    # No server-side search tool on this API.
    supports_documents = False

    def complete(self, system, messages, temperature, max_tokens,
                 documents=None, document_fallback="", cache_prefix="",
                 web_search=False):
        # Documents fall back to their extracted text; search is not offered.
        return super().complete(
            system, messages, temperature, max_tokens,
            None, document_fallback or cache_prefix, cache_prefix, False,
        )


class CustomProvider(OpenAIProvider):
    """Any OpenAI-compatible endpoint: OpenRouter, Groq, Together, vLLM,
    Ollama (http://localhost:11434/v1), LM Studio, your own gateway."""

    key = "custom"
    label = "Custom model"
    default_model = ""
    needs_base_url = True
    # Self-hosted gateways rarely accept file parts. Documents are attempted
    # anyway and fall back to text on the first refusal, which costs one
    # wasted call per model rather than a configuration setting.
    supports_documents = True

    def complete(self, system, messages, temperature, max_tokens,
                 documents=None, document_fallback="", cache_prefix="",
                 web_search=False):
        if not self.base_url:
            raise ProviderError("This custom agent has no base URL set.")
        return super().complete(
            system, messages, temperature, max_tokens, documents,
            document_fallback, cache_prefix, web_search,
        )


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


def _deepseek_list(self):
    data = self._get(
        f"{self.api_base}/models",
        headers={"Authorization": f"Bearer {self.api_key}"},
    )
    rows = data.get("data", [])
    return [
        {"id": row.get("id"), "label": row.get("id")}
        for row in rows if row.get("id")
    ]


def _custom_list(self):
    if not self.base_url:
        raise ProviderError("This custom agent has no base URL set.")
    return _openai_list(self)


AnthropicProvider.list_models = _anthropic_list
DeepSeekProvider.list_models = _deepseek_list
OpenAIProvider.list_models = _openai_list
GeminiProvider.list_models = _gemini_list
CustomProvider.list_models = _custom_list
