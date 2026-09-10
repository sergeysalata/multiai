#!/usr/bin/env python3
"""multiai.online — start with:  python app.py

First run does the setup for you:
  * creates config.json from config.example.json if it is missing
  * generates security.secret_key and security.encryption_key if empty
  * checks the database connection and that the tables exist
then serves on server.host:server.port from config.json (127.0.0.1:30303).

Note: this file is app.py and the package next to it is app/. Python resolves
the package first, so "from app import create_app" below loads app/__init__.py,
not this file. Running "python app.py" still works normally.
"""

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
EXAMPLE = ROOT / "config.example.json"


def die(message, hint=""):
    print(f"\n  {message}")
    if hint:
        print(f"  {hint}")
    print()
    sys.exit(1)


def ensure_config():
    """Create config.json and fill in any missing secrets. Returns the config dict."""
    created = False
    if not CONFIG.is_file():
        if not EXAMPLE.is_file():
            die("No config.json and no config.example.json to copy from.")
        shutil.copy(EXAMPLE, CONFIG)
        created = True
        print(f"  Created {CONFIG.name} from the example.")

    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        die(f"config.json is not valid JSON: {exc}", "Check for a trailing comma.")

    security = data.setdefault("security", {})
    generated = []

    if not security.get("secret_key"):
        import secrets

        security["secret_key"] = secrets.token_hex(32)
        generated.append("secret_key")

    if not security.get("encryption_key"):
        from cryptography.fernet import Fernet

        security["encryption_key"] = Fernet.generate_key().decode()
        generated.append("encryption_key")

    if generated:
        CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  Generated {' and '.join(generated)} and saved them to config.json.")
        if "encryption_key" in generated:
            print("  Back that file up. Losing encryption_key makes every stored")
            print("  provider API key permanently unreadable.")

    # Secrets on disk should not be world-readable.
    try:
        mode = CONFIG.stat().st_mode
        if mode & (stat.S_IRGRP | stat.S_IROTH):
            CONFIG.chmod(0o600)
    except OSError:
        pass

    db = data.get("database", {})
    if db.get("password") in ("", "change-this-password"):
        die(
            "database.password in config.json is still the placeholder.",
            "Set it to your Postgres password for the 'multiai' user, then run again.",
        )

    if created:
        print("  Review the rest of config.json when you get a chance.\n")

    return data


# Columns added by later migrations. Checked by name so a skipped migration
# is reported plainly here, rather than surfacing later as a confusing runtime
# error — Postgres reports a missing "discussions.mode" as "WITHIN GROUP is
# required for ordered-set aggregate mode", because it falls back to looking
# for the built-in mode() function.
REQUIRED_COLUMNS = {
    ("users", "google_sub"): "sql/002_google_auth.sql",
    ("users", "avatar_url"): "sql/002_google_auth.sql",
    ("discussions", "mode"): "sql/003_rooms.sql",
    ("attachments", "extracted"): "sql/004_files.sql",
    ("agents", "avatar_preset"): "sql/005_avatars.sql",
    ("agents", "web_search"): "sql/006_tools.sql",
    ("groups", "auto_stop"): "sql/007_flow_stop.sql",
    ("users", "about"): "sql/009_profile.sql",
}


def check_database(app, db):
    """Confirm Postgres is reachable and every migration has been applied."""
    expected = {
        "users", "credentials", "agents", "groups",
        "group_members", "discussions", "messages",
    }
    with app.app_context():
        try:
            found = set(
                db.session.execute(
                    db.text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                ).scalars().all()
            )
        except Exception as exc:  # noqa: BLE001
            die(
                f"Cannot reach the database: {exc}",
                "Check host, port, name, user and password under \"database\" in config.json.",
            )

        missing = expected - found
        if missing:
            die(
                "These tables are missing: " + ", ".join(sorted(missing)),
                "Run sql/001_schema.sql in pgAdmin against the multiai database.",
            )

        columns = set(
            db.session.execute(
                db.text(
                    "SELECT table_name || '.' || column_name "
                    "FROM information_schema.columns WHERE table_schema = 'public'"
                )
            ).scalars().all()
        )
        pending = {}
        for (table, column), script in REQUIRED_COLUMNS.items():
            if f"{table}.{column}" not in columns:
                pending.setdefault(script, []).append(f"{table}.{column}")

        if pending:
            lines = [
                f"{script} — adds {', '.join(sorted(cols))}"
                for script, cols in sorted(pending.items())
            ]
            die(
                "The database is behind the code. Missing migration(s):\n    "
                + "\n    ".join(lines),
                "Run them in pgAdmin against the multiai database, then start again.",
            )

    print("  Database connected, schema up to date.")


def main():
    parser = argparse.ArgumentParser(description="Run the multiai backend.")
    parser.add_argument("--host", help="Override server.host from config.json")
    parser.add_argument("--port", type=int, help="Override server.port from config.json")
    parser.add_argument("--debug", action="store_true",
                        help="Auto-reload on code changes. Never use in production.")
    parser.add_argument("--skip-checks", action="store_true",
                        help="Skip the database check and start immediately.")
    args = parser.parse_args()

    print("\n  multiai.online")
    print("  " + "-" * 40)

    ensure_config()

    try:
        from app import create_app
        from app.config import Config
        from app.extensions import db
    except ModuleNotFoundError as exc:
        die(
            f"Missing dependency: {exc.name}",
            "Install them:  pip install -r requirements.txt",
        )

    config = Config()
    application = create_app(config)

    if not args.skip_checks:
        check_database(application, db)

    if config.LOG_TO_FILE:
        print(f"  Logs in {config.LOG_DIR}/ (multiai.log, room.log)")

    limit_mb = (config.MAX_CONTENT_LENGTH or 0) / 1024 / 1024
    print(f"  Upload limit {limit_mb:.0f} MB "
          f"(Apache LimitRequestBody must be at least {config.MAX_CONTENT_LENGTH})")

    upload_dir = config.UPLOAD_DIR
    try:
        os.makedirs(upload_dir, exist_ok=True)
        probe = os.path.join(upload_dir, ".write-test")
        with open(probe, "w") as handle:
            handle.write("ok")
        os.remove(probe)
        print(f"  Uploads go to {upload_dir}")
    except OSError as exc:
        die(
            f"Cannot write to the upload directory {upload_dir}: {exc}",
            'Fix the permissions, or set "storage": {"upload_dir": "..."} in config.json.',
        )

    host = args.host or config.HOST
    port = args.port or config.PORT
    prefix = config.URL_PREFIX or ""

    print(f"  Serving on http://{host}:{port}{prefix}/")
    if config.PUBLIC_ORIGIN:
        print(f"  Public URL  {config.PUBLIC_ORIGIN}{prefix}/  (via Apache)")
    print("  Ctrl+C to stop.\n")

    # threaded=True is required: each live debate holds one thread for the
    # orchestrator and one for the event stream.
    application.run(
        host=host,
        port=port,
        debug=args.debug,
        threaded=True,
        use_reloader=args.debug,
    )


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
