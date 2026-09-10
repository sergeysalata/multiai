"""Configuration comes from config.json in the project root.

Lookup order for the file:
    1. $MULTIAI_CONFIG (absolute path)
    2. <project root>/config.json
    3. <project root>/config.example.json  (development fallback)

Individual secrets can still be overridden by environment variables, which is
useful when the process is managed by systemd and you would rather not have
the key on disk: MULTIAI_SECRET_KEY, MULTIAI_ENCRYPTION_KEY, MULTIAI_DB_PASSWORD.
"""

import json
import os
from pathlib import Path
from urllib.parse import quote_plus

ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


def config_path() -> Path:
    override = os.environ.get("MULTIAI_CONFIG")
    if override:
        p = Path(override)
        if not p.is_file():
            raise ConfigError(f"MULTIAI_CONFIG points at {p}, which does not exist.")
        return p
    for candidate in (ROOT / "config.json", ROOT / "config.example.json"):
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"No config.json found in {ROOT}. Copy config.example.json to config.json "
        "and fill it in."
    )


def load_file() -> dict:
    path = config_path()
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from exc


def build_database_uri(db: dict) -> str:
    user = quote_plus(str(db.get("user", "multiai")))
    password = quote_plus(str(os.environ.get("MULTIAI_DB_PASSWORD") or db.get("password", "")))
    host = db.get("host", "127.0.0.1")
    port = db.get("port", 5432)
    name = db.get("name", "multiai")
    sslmode = db.get("sslmode", "prefer")
    auth = f"{user}:{password}" if password else user
    return f"postgresql+psycopg://{auth}@{host}:{port}/{name}?sslmode={sslmode}"


class Config:
    """Flask config object built from config.json."""

    def __init__(self, raw=None):
        raw = raw if raw is not None else load_file()
        server = raw.get("server", {})
        database = raw.get("database", {})
        security = raw.get("security", {})
        limits = raw.get("limits", {})
        providers = raw.get("providers", {})

        # --- raw sections kept for the CLI and gunicorn ---
        self.RAW = raw
        self.SERVER = server
        logging_cfg = raw.get("logging", {})
        self.LOG_LEVEL = logging_cfg.get("level", "INFO")
        # Logs land in ./logs next to app.py unless a path is given. Relative
        # paths are resolved against the project root, so a stray "logs" in
        # config.json does not scatter files into whatever the working
        # directory happened to be.
        log_dir = logging_cfg.get("dir") or "logs"
        self.LOG_DIR = str(
            Path(log_dir) if os.path.isabs(log_dir) else ROOT / log_dir
        )
        self.LOG_TO_FILE = logging_cfg.get("to_file", True)
        self.LOG_MAX_BYTES = int(logging_cfg.get("max_bytes", 20 * 1024 * 1024))
        self.LOG_BACKUPS = int(logging_cfg.get("backups", 5))

        # --- Flask core ---
        self.SECRET_KEY = (
            os.environ.get("MULTIAI_SECRET_KEY")
            or security.get("secret_key")
            or ""
        )
        self.HOST = server.get("host", "127.0.0.1")
        self.PORT = int(server.get("port", 30303))
        self.URL_PREFIX = (server.get("url_prefix") or "").rstrip("/")
        self.PUBLIC_ORIGIN = server.get("public_origin", "https://multiai.online")
        self.APPLICATION_ROOT = self.URL_PREFIX or "/"
        self.PREFERRED_URL_SCHEME = "https"
        self.MAX_CONTENT_LENGTH = int(limits.get("max_upload_bytes", 2 * 1024 * 1024))

        # --- session cookies ---
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"
        self.SESSION_COOKIE_SECURE = bool(security.get("session_cookie_secure", True))
        self.SESSION_COOKIE_PATH = self.URL_PREFIX or "/"
        self.MIN_PASSWORD_LENGTH = int(security.get("min_password_length", 10))

        # --- database ---
        self.SQLALCHEMY_DATABASE_URI = build_database_uri(database)
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        self.SQLALCHEMY_ENGINE_OPTIONS = {
            "pool_pre_ping": True,
            "pool_recycle": int(database.get("pool_recycle_seconds", 280)),
            "pool_size": int(database.get("pool_size", 5)),
            "max_overflow": int(database.get("max_overflow", 10)),
        }

        # --- encryption ---
        self.ENCRYPTION_KEY = (
            os.environ.get("MULTIAI_ENCRYPTION_KEY")
            or security.get("encryption_key")
            or ""
        )

        # --- limits ---
        self.MAX_ROUNDS = int(limits.get("max_rounds", 4))
        self.MAX_AGENTS_PER_GROUP = int(limits.get("max_agents_per_group", 6))
        self.PROVIDER_TIMEOUT = int(limits.get("provider_timeout_seconds", 120))
        self.MAX_TRANSCRIPT_CHARS = int(limits.get("max_transcript_chars", 24000))
        # How many agent turns a free-running conversation takes before it
        # pauses for you. Purely a spend guard — press Resume for more.
        self.MAX_FLOW_TURNS = int(limits.get("max_flow_turns", 40))
        # Per file, and for all files put together in one prompt. Files can
        # dwarf the conversation, so they get their own budget.
        self.MAX_FILE_CHARS = int(limits.get("max_file_chars", 200000))
        self.MAX_FILES_CHARS_TOTAL = int(limits.get("max_files_chars_total", 400000))
        self.MAX_DOCUMENT_BYTES = int(limits.get("max_document_bytes", 24 * 1024 * 1024))
        self.MAX_FILES_PER_TOPIC = int(limits.get("max_files_per_topic", 10))

        storage = raw.get("storage", {})
        self.UPLOAD_DIR = storage.get("upload_dir") or str(ROOT / "uploads")

        # --- optional server-wide provider keys ---
        # Send PDFs to the vendor as real documents rather than as extracted
        # text, where the vendor supports it. Keeps tables and layout.
        self.NATIVE_DOCUMENTS = bool(providers.get("native_documents", True))

        self.FALLBACK_KEYS = {
            k: (v or "") for k, v in (providers.get("fallback_keys") or {}).items()
        }

        # --- Google sign-in ---
        google = raw.get("google_oauth", {})
        self.GOOGLE_OAUTH = {
            "enabled": bool(google.get("enabled")),
            "client_id": os.environ.get("MULTIAI_GOOGLE_CLIENT_ID")
            or google.get("client_id", ""),
            "client_secret": os.environ.get("MULTIAI_GOOGLE_CLIENT_SECRET")
            or google.get("client_secret", ""),
            "redirect_uri": google.get("redirect_uri")
            or f"{self.PUBLIC_ORIGIN}{self.URL_PREFIX}/v1/auth/google/callback",
            "allowed_domains": [
                d.strip().lower() for d in (google.get("allowed_domains") or []) if d.strip()
            ],
        }
        if self.GOOGLE_OAUTH["enabled"] and not (
            self.GOOGLE_OAUTH["client_id"] and self.GOOGLE_OAUTH["client_secret"]
        ):
            self.GOOGLE_OAUTH["enabled"] = False
            self.GOOGLE_OAUTH["disabled_reason"] = (
                "google_oauth.enabled is true but client_id or client_secret is empty."
            )

    def warnings(self):
        """Problems worth logging loudly at boot rather than failing on."""
        out = []
        if not self.SECRET_KEY:
            out.append(
                "security.secret_key is empty — sessions will not survive a restart. "
                'Generate one: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        if not self.ENCRYPTION_KEY:
            out.append(
                "security.encryption_key is empty — saving provider keys will fail. "
                "Generate one: flask new-encryption-key"
            )
        if "change-this-password" in self.SQLALCHEMY_DATABASE_URI:
            out.append("database.password is still the placeholder from the example file.")
        if self.GOOGLE_OAUTH.get("disabled_reason"):
            out.append(
                self.GOOGLE_OAUTH["disabled_reason"] + " Google sign-in stays off."
            )
        return out
