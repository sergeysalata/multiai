#!/usr/bin/env bash
# Start the multiai backend. Intended for: screen -S multiai ./run.sh
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "No .venv here. Run:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

if [ ! -f config.json ]; then
    echo "No config.json. Run:  cp config.example.json config.json  and fill it in."
    exit 1
fi

source .venv/bin/activate
export FLASK_APP=wsgi.py

# Fail fast on a bad database or a missing table, rather than on first request.
flask check-db

exec gunicorn -c deploy/gunicorn.conf.py wsgi:app
