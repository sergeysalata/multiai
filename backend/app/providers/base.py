"""Provider adapters.

Every adapter takes the same neutral input — a system prompt plus an
alternating message list — and returns plain text. That is what lets one
model's output be handed to a different vendor's model as input.
"""

import requests


class ProviderError(RuntimeError):
    """Raised when a vendor call fails in a way worth showing the user."""


class BaseProvider:
    key = "base"
    label = "Base"
    default_model = ""
    needs_base_url = False

    def __init__(self, api_key: str, model: str, base_url: str = "", timeout: int = 120):
        self.api_key = (api_key or "").strip()
        self.model = (model or self.default_model).strip()
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = timeout
        # Filled in by complete(): stop reason and token counts for the log.
        self.last_meta = {}
        # The vendor's raw error body, kept for the log when the message alone
        # says nothing useful.
        self.last_error_body = ""

    # Whether this vendor can take a PDF as a document rather than as text.
    supports_documents = False

    def complete(self, system: str, messages: list[dict], temperature: float,
                 max_tokens: int, documents: list[dict] | None = None,
                 document_fallback: str = "", cache_prefix: str = "",
                 web_search: bool = False) -> str:
        """Answer the prompt.

        `documents` is a neutral list of
        {"filename", "media_type", "data" (base64)} that the vendor parses
        itself — layout, tables and figures survive, which plain-text
        extraction loses. `document_fallback` is the extracted text for those
        same files, used automatically if the vendor refuses the documents, so
        a model that cannot take PDFs still sees their contents.

        `cache_prefix` is the part of the prompt that does not change between
        turns — the shared files. Keeping it first and marking it lets vendors
        reuse it instead of re-reading a large document on every single turn,
        which is where the money goes in a long conversation.

        `web_search` asks the vendor to enable its own server-side search tool.
        The model decides whether to use it, the vendor runs it, and the result
        arrives inside the same reply — nothing is executed here. A vendor or
        model that will not accept the tool is retried without it.
        """
        raise NotImplementedError

    def list_models(self) -> list[dict]:
        """Ask the vendor which models this key can actually use.

        Returns [{"id": ..., "label": ...}, ...]. Raises ProviderError if the
        key is wrong or the endpoint is unreachable — the caller falls back to
        the hard-coded suggestions rather than showing an empty picker.
        """
        raise NotImplementedError

    # -- helpers -------------------------------------------------------
    def _get(self, url, *, headers=None, params=None):
        try:
            resp = requests.get(url, headers=headers, params=params, timeout=self.timeout)
        except requests.Timeout as exc:
            raise ProviderError(f"{self.label} did not answer in time.") from exc
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach {self.label}: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(self._explain(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.label} returned a non-JSON body.") from exc

    def _post(self, url, *, headers=None, json=None, params=None):
        try:
            resp = requests.post(
                url, headers=headers, json=json, params=params, timeout=self.timeout
            )
        except requests.Timeout as exc:
            raise ProviderError(
                f"{self.label} did not answer within {self.timeout}s."
            ) from exc
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach {self.label}: {exc}") from exc

        if resp.status_code >= 400:
            raise ProviderError(self._explain(resp))
        try:
            return resp.json()
        except ValueError as exc:
            raise ProviderError(f"{self.label} returned a non-JSON body.") from exc

    def _explain(self, resp):
        detail = ""
        try:
            body = resp.json()
            error = body.get("error")
            if isinstance(error, dict):
                detail = error.get("message") or ""
                # Google puts the useful part in details[], not message.
                extra = error.get("details") or error.get("status")
                if extra and detail in ("", "Internal error encountered."):
                    detail = f"{detail} {extra}".strip()
            else:
                detail = error or body.get("message") or ""
        except ValueError:
            detail = resp.text[:300]

        # A bare "Internal error encountered." is useless on its own; keep the
        # raw body so the log says something actionable.
        self.last_error_body = (resp.text or "")[:600]
        if resp.status_code in (401, 403):
            return f"{self.label} rejected the API key. {detail}".strip()
        if resp.status_code == 404:
            return f"{self.label} has no model named '{self.model}'. {detail}".strip()
        if resp.status_code == 429:
            return f"{self.label} rate limit hit. {detail}".strip()
        return f"{self.label} returned {resp.status_code}. {detail}".strip()
