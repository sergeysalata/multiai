# multiai.online

Flask + Postgres. Several AI models sit in one group, read each other's
answers, argue, and hand back a single conclusion.

The design turns on one thing: **agents speak in sequence, and every turn one
model produces becomes input for every model that speaks after it.** Nothing
runs in parallel.

```
Round 1  seat 1 (Claude)   input: your question
         seat 2 (Gemini)   input: your question + Claude's turn
         seat 3 (ChatGPT)  input: your question + Claude + Gemini
Round 2  seat 1 (Claude)   input: the entire round-1 record
         ...                cross-examination: challenge, then update
Verdict  the chair          input: the whole transcript
```

Each turn is committed to Postgres the moment it lands, then re-serialised
into the next prompt as quoted speech with a name attached:

```
[Round 1] Ada (Claude · claude-sonnet-4-5) said:
  …the migration should be online, because…
```

---

## Runtime shape

| | |
|---|---|
| Backend | gunicorn on `127.0.0.1:30303`, mounted at `/api` |
| Public | `https://multiai.online/api/…` via Apache reverse proxy |
| Database | Postgres, database `multiai` |
| Config | `config.json` in the project root |

Both the HTML pages and the JSON/SSE endpoints live under `/api`, so Apache
needs exactly one proxy rule. `PrefixMiddleware` moves the prefix into
`SCRIPT_NAME`, which keeps `url_for()` generating correct public links — no
paths are hard-coded anywhere. To serve the app at the domain root instead,
set `server.url_prefix` to `""` and proxy `/`.

## Layout

```
config.json          all connection and tuning parameters
sql/001_schema.sql   run this in pgAdmin
sql/999_reset.sql    drops everything
app/
  config.py          reads config.json, builds the DSN
  __init__.py        app factory, PrefixMiddleware, ProxyFix, CLI
  models.py          User, Credential, Agent, Group, GroupMember,
                     Discussion, Message
  orchestrator.py    the relay engine — prompts, rounds, verdict
  providers/         one adapter per vendor, identical signatures
  rendering.py       Markdown → sanitised HTML (model output is untrusted)
  crypto.py          Fernet encryption for stored API keys
  routes/            auth, pages, JSON + SSE API
  templates/ static/
deploy/              apache vhost, gunicorn conf, systemd unit
wsgi.py
```

## 1. Database

In pgAdmin, connected to **postgres**, create the role and database:

```sql
CREATE ROLE multiai WITH LOGIN PASSWORD 'your-password';
CREATE DATABASE multiai WITH OWNER multiai ENCODING 'UTF8';
```

Reconnect to **multiai**, open `sql/001_schema.sql` in the Query Tool and
execute it, then `sql/002_google_auth.sql`. It is idempotent, so re-running is harmless. Verify:

```sql
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public' ORDER BY table_name;
-- agents, credentials, discussions, group_members, groups,
-- messages, schema_version, users
```

`CREATE DATABASE` cannot run inside a transaction block, which is why those
two lines are not bundled into the schema file.

## 2. Config

```bash
cp config.example.json config.json     # or just let `python app.py` do it
```

Set one value:

```json
"database": { "password": "your-password" }
```

`secret_key` and `encryption_key` are filled in automatically on first run.
`secret_key` signs session cookies — changing it logs everyone out.
`encryption_key` is the Fernet key protecting stored provider API keys —
changing it makes every saved key undecryptable, so back up `config.json`
separately from the database.

If you would rather not keep secrets on disk, leave them empty and set
`MULTIAI_SECRET_KEY`, `MULTIAI_ENCRYPTION_KEY` and `MULTIAI_DB_PASSWORD` in the
systemd unit; environment values win over the file.

## 3. Run

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python app.py           # http://127.0.0.1:30303/api/
```

First run creates `config.json`, generates `secret_key` and `encryption_key`,
chmods the file to 600, and checks the database before serving. The only thing
you must fill in by hand is `database.password`.

`python app.py` uses the built-in server — fine for getting started and for
light traffic behind Apache. For real load use `./run.sh`, which runs the same
app under gunicorn with threaded workers.

Then: **Keys** → paste a provider key → **Agents** → create two or three from
different vendors → **Groups** → new group → seat them → ask.

## 4. Deploy to multiai.online

```bash
sudo apt install -y python3-venv postgresql apache2 certbot python3-certbot-apache
sudo adduser --system --group --home /srv/multiai multiai

sudo -u multiai git clone <your-repo> /srv/multiai
cd /srv/multiai
sudo -u multiai python3 -m venv .venv
sudo -u multiai .venv/bin/pip install -r requirements.txt
sudo -u multiai cp config.example.json config.json && sudo -u multiai nano config.json
sudo chmod 600 /srv/multiai/config.json

sudo cp deploy/multiai.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now multiai
curl http://127.0.0.1:30303/healthz        # {"ok": true, "database": "up"}

sudo a2enmod ssl proxy proxy_http headers rewrite http2
sudo cp deploy/apache-multiai.online.conf /etc/apache2/sites-available/multiai.online.conf
sudo a2ensite multiai.online
sudo certbot --apache -d multiai.online -d www.multiai.online
sudo apachectl configtest && sudo systemctl reload apache2
```

DNS: A records for `multiai.online` and `www` → server IP.

Three settings are load-bearing and easy to miss:

- **`SetEnv no-gzip 1` on `/api/discussions`** — mod_deflate buffers the event
  stream, and the debate looks frozen until it finishes.
- **`ProxyTimeout 1800`** — a four-round debate across slow models outlives the
  60-second default.
- **`worker_class = "gthread"`** in gunicorn — each debate holds one thread for
  the orchestrator and one for the stream. Sync workers deadlock.

Add an hourly cron entry so a restart mid-debate does not leave rows stuck:

```
0 * * * * cd /srv/multiai && FLASK_APP=wsgi.py .venv/bin/flask release-stuck
```

## Google sign-in

Optional. Off until you fill in `google_oauth` in `config.json`.

1. Google Cloud console → **APIs & Services → Credentials → Create credentials →
   OAuth client ID → Web application**.
2. Authorised redirect URI, exactly:
   `https://multiai.online/api/v1/auth/google/callback`
3. Authorised JavaScript origin: `https://multiai.online`
4. Put the client ID and secret in `config.json` and set `"enabled": true`.
5. Run `sql/002_google_auth.sql` in pgAdmin, then restart the backend.

```json
"google_oauth": {
  "enabled": true,
  "client_id": "....apps.googleusercontent.com",
  "client_secret": "....",
  "redirect_uri": "https://multiai.online/api/v1/auth/google/callback",
  "allowed_domains": []
}
```

`allowed_domains` restricts sign-in to particular email domains — leave it empty
to allow anyone. If the client id or secret is missing, the server logs a
warning and quietly keeps Google sign-in switched off rather than showing a
button that fails.

The flow is the standard authorisation-code exchange with a `state` check, and
identity comes from Google's userinfo endpoint over TLS rather than from
verifying the ID token locally — one fewer thing to get wrong. If someone
already registered with a password using the same address, the Google identity
is linked to that account instead of creating a second one.

## Live model lists

The agent form asks the provider which models the selected key can actually
use: `GET /v1/models` on Anthropic, `/v1/models` on OpenAI, `/v1beta/models` on
Google, and `{base_url}/models` for custom endpoints. Answers are cached per key
for five minutes.

Non-conversational models are filtered out — embeddings, whisper, TTS, image and
moderation endpoints cannot take a seat on a panel. If a key is missing, wrong,
or the vendor is unreachable, the picker falls back to a short built-in list and
says why, and "Type a name" always lets you enter a model by hand.

## Design notes and limits

**Background work is threads, not a queue.** Fine for tens of concurrent
debates. Past that, move `run_discussion` onto Celery or RQ; it already takes
`(app, discussion_id)` and keeps all state in Postgres, so the move is
mechanical.

**Context grows every turn**, since each speaker reads everything before it.
`limits.max_transcript_chars` trims from the oldest end. The group page shows
the call count before you commit: agents × rounds + 1.

**One agent failing does not stop the room.** The failure is stored as a turn,
the others are told that member could not answer, and the debate continues.
Only a total wipeout aborts before the verdict.

**Security.** Provider keys are Fernet-encrypted, so a database dump alone
leaks nothing. Model output goes through bleach with a small tag allowlist,
because a model can be talked into emitting `<script>`. Every route checks
ownership against `current_user`. `config.json` should be `chmod 600`.

**Not built yet**, in the order I would add them: CSRF tokens on the HTML forms
(Flask-WTF), per-user rate limiting, group sharing between accounts, an admin
view.
