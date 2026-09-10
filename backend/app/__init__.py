import logging

from flask import Flask, jsonify, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .extensions import db, login_manager, migrate
from .providers import provider_label


class PrefixMiddleware:
    """Mount the whole app under a path prefix, e.g. /api.

    Apache proxies https://multiai.online/api → 127.0.0.1:30303/api, so the
    prefix arrives in PATH_INFO. Moving it into SCRIPT_NAME means url_for()
    keeps generating correct public links without any hard-coded strings.
    Requests that do not carry the prefix (a local curl to /healthz, or an
    Apache rule that strips it) still work.
    """

    def __init__(self, app, prefix=""):
        self.app = app
        self.prefix = (prefix or "").rstrip("/")

    def __call__(self, environ, start_response):
        if self.prefix:
            path = environ.get("PATH_INFO", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + self.prefix
                environ["PATH_INFO"] = path[len(self.prefix):] or "/"
        return self.app(environ, start_response)


def create_app(config_object=None):
    app = Flask(__name__)
    cfg = config_object or Config()
    app.config.from_object(cfg)

    # Trust exactly one proxy hop (Apache) for scheme, host and client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    app.wsgi_app = PrefixMiddleware(app.wsgi_app, app.config.get("URL_PREFIX", ""))

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from .routes.api import bp as api_bp
    from .routes.api_v1 import bp as api_v1_bp
    from .routes.auth import bp as auth_bp
    from .routes.main import bp as main_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(api_v1_bp)

    from .rendering import render as render_markdown

    app.jinja_env.filters["provider_label"] = provider_label
    app.jinja_env.filters["md"] = render_markdown

    @app.errorhandler(413)
    def too_large(_):
        """Say the actual limit. "Too large" without a number is useless."""
        limit = app.config.get("MAX_CONTENT_LENGTH") or 0
        megabytes = limit / 1024 / 1024
        message = (
            f"That upload is over the {megabytes:.0f} MB limit. Raise "
            '"limits.max_upload_bytes" in config.json and restart, and make '
            "sure Apache's LimitRequestBody is at least as large."
        )
        if request.path.startswith("/v1") or request.accept_mimetypes.accept_json:
            return jsonify({"error": message}), 413
        return render_template("error.html", code=413, message=message), 413

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", code=404,
                               message="That page does not exist."), 404

    @app.errorhandler(500)
    def server_error(_):
        db.session.rollback()
        return render_template("error.html", code=500,
                               message="Something broke on our side."), 500

    @app.get("/healthz")
    def healthz():
        """Liveness probe. Also confirms Postgres is reachable."""
        try:
            db.session.execute(db.text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "database": str(exc)}, 503
        return {"ok": True, "database": "up"}

    register_cli(app)

    level = getattr(logging, str(app.config.get("LOG_LEVEL", "INFO")).upper(), 20)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if app.config.get("LOG_TO_FILE", True):
        _attach_file_logging(app, level)

    app.logger.info(
        "multiai ready on %s:%s, mounted at %s",
        app.config["HOST"], app.config["PORT"], app.config["URL_PREFIX"] or "/",
    )
    return app


def _attach_file_logging(app, level):
    """Two files in the logs directory.

    multiai.log — everything, for tracebacks and request errors.
    room.log    — just the conversation: who spoke, how long it took, what
                  came back. This is the one worth tailing while a chat runs,
                  because it is not buried in HTTP noise.
    """
    import os
    from logging.handlers import RotatingFileHandler

    directory = app.config.get("LOG_DIR")
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        app.logger.warning("Could not create the log directory %s: %s", directory, exc)
        return

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    max_bytes = app.config.get("LOG_MAX_BYTES", 20 * 1024 * 1024)
    backups = app.config.get("LOG_BACKUPS", 5)

    def build(filename, logger, propagate=True):
        path = os.path.join(directory, filename)
        try:
            handler = RotatingFileHandler(
                path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
        except OSError as exc:
            app.logger.warning("Could not open %s: %s", path, exc)
            return None
        handler.setFormatter(formatter)
        handler.setLevel(level)
        logger.addHandler(handler)
        logger.propagate = propagate
        return path

    main_path = build("multiai.log", logging.getLogger())
    room_logger = logging.getLogger("multiai.room")
    room_logger.setLevel(level)
    # propagate stays on, so room lines appear in both files and on stdout.
    room_path = build("room.log", room_logger)

    app.config["LOG_FILES"] = [p for p in (main_path, room_path) if p]
    if main_path:
        app.logger.info("Logging to %s", directory)


def register_cli(app):
    @app.cli.command("init-db")
    def init_db():
        """Create tables from the models. Prefer sql/001_schema.sql in production."""
        db.create_all()
        print("Tables created.")

    @app.cli.command("check-db")
    def check_db():
        """Verify the connection and that every expected table exists."""
        expected = {
            "users", "credentials", "agents", "groups",
            "group_members", "discussions", "messages",
        }
        required_columns = {
            ("users", "google_sub"): "sql/002_google_auth.sql",
            ("users", "avatar_url"): "sql/002_google_auth.sql",
            ("discussions", "mode"): "sql/003_rooms.sql",
            ("attachments", "extracted"): "sql/004_files.sql",
        }
        found = set(
            db.session.execute(
                db.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalars().all()
        )
        columns = set(
            db.session.execute(
                db.text(
                    "SELECT table_name || '.' || column_name "
                    "FROM information_schema.columns WHERE table_schema = 'public'"
                )
            ).scalars().all()
        )
        print(f"Connected. Tables found: {len(found)}")
        missing = expected - found
        if missing:
            print("MISSING TABLES: " + ", ".join(sorted(missing)))
            print("Run sql/001_schema.sql against the multiai database.")

        pending = sorted(
            {
                script
                for (table, column), script in required_columns.items()
                if f"{table}.{column}" not in columns
            }
        )
        if pending:
            print("MIGRATIONS NOT APPLIED: " + ", ".join(pending))
        elif not missing:
            print("Schema up to date.")

    @app.cli.command("new-encryption-key")
    def new_key():
        """Print a Fernet key for security.encryption_key in config.json."""
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())

    @app.cli.command("new-secret-key")
    def new_secret():
        """Print a value for security.secret_key in config.json."""
        import secrets

        print(secrets.token_hex(32))

    @app.cli.command("release-stuck")
    def release_stuck():
        """Fail discussions left 'running' by a restart. Run hourly from cron."""
        from datetime import datetime, timedelta

        from .models import Discussion

        cutoff = datetime.utcnow() - timedelta(hours=1)
        stuck = Discussion.query.filter(
            Discussion.status.in_(("queued", "running")),
            Discussion.created_at < cutoff,
        ).all()
        for d in stuck:
            d.status = "failed"
            d.stage = "Stopped"
            d.error = "The server restarted while this debate was running."
            d.finished_at = datetime.utcnow()
        db.session.commit()
        print(f"Released {len(stuck)} stuck discussion(s).")
