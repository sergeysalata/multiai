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

    def complete(self, system: str, messages: list[dict], temperature: float,
                 max_tokens: int) -> str:
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
            detail = (
                body.get("error", {}).get("message")
                if isinstance(body.get("error"), dict)
                else body.get("error") or body.get("message") or ""
            )
        except ValueError:
            detail = resp.text[:300]
        if resp.status_code in (401, 403):
            return f"{self.label} rejected the API key. {detail}".strip()
        if resp.status_code == 404:
            return f"{self.label} has no model named '{self.model}'. {detail}".strip()
        if resp.status_code == 429:
            return f"{self.label} rate limit hit. {detail}".strip()
        return f"{self.label} returned {resp.status_code}. {detail}".strip()
