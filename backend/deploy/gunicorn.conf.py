"""Gunicorn settings, read from config.json so there is one source of truth.

    gunicorn -c deploy/gunicorn.conf.py wsgi:app

Threads are load-bearing: a live debate holds one thread for the SSE stream
and one for the orchestrator, so the default sync worker would deadlock.
"""

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("MULTIAI_CONFIG", ROOT / "config.json"))
if not CONFIG.is_file():
    CONFIG = ROOT / "config.example.json"

with CONFIG.open(encoding="utf-8") as fh:
    _server = json.load(fh).get("server", {})

bind = f"{_server.get('host', '127.0.0.1')}:{_server.get('port', 30303)}"
workers = int(_server.get("workers", 3))
threads = int(_server.get("threads", 16))
worker_class = "gthread"

timeout = int(_server.get("request_timeout_seconds", 180))
graceful_timeout = 60
keepalive = 75                 # must exceed Apache's KeepAliveTimeout
max_requests = 1000
max_requests_jitter = 100

chdir = str(ROOT)
accesslog = "-"
errorlog = "-"
loglevel = "info"
