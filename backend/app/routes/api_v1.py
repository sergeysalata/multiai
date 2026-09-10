"""JSON API consumed by the static site in /var/www/multiai.online.

Public paths are https://multiai.online/api/v1/... — this blueprint adds
"/v1" on top of the "/api" mount applied by PrefixMiddleware.

Auth is the same Flask-Login session cookie the server-rendered pages use.
Because the cookie travels automatically, every mutating request must also
carry the X-CSRF-Token header, which the client reads from GET /v1/session.
"""

import os
import secrets
import time
from functools import wraps
from urllib.parse import urlencode

import requests
from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    request,
    send_file,
    session,
)
from flask_login import current_user, login_user, logout_user

from ..crypto import EncryptionNotConfigured, encrypt, mask
from ..extensions import db
from ..files import extract, safe_name
from ..models import (
    Agent,
    Attachment,
    Credential,
    Discussion,
    Group,
    GroupMember,
    Message,
)
from ..orchestrator import start_flow, start_turns
from ..providers import (
    PROVIDER_CHOICES,
    REGISTRY,
    SUGGESTED_MODELS,
    ProviderError,
    get_provider,
)
from ..rendering import render
from ..routes.auth import EMAIL_RE
from ..routes.main import SEAT_COLORS

bp = Blueprint("v1", __name__, url_prefix="/v1")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


# ------------------------------------------------------------------ plumbing
def csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
        session.permanent = True
    return token


@bp.before_request
def check_csrf():
    if request.method in SAFE_METHODS:
        return None
    sent = request.headers.get("X-CSRF-Token", "")
    expected = session.get("csrf_token", "")
    if not expected or not sent or not secrets.compare_digest(sent, expected):
        return jsonify({"error": "Session expired. Reload the page and try again."}), 403
    return None


def auth_required(view):
    """Like login_required, but answers with JSON instead of a redirect."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Sign in to continue.", "code": "unauthenticated"}), 401
        return view(*args, **kwargs)

    return wrapper


def body():
    return request.get_json(silent=True) or {}


def fail(message, status=400):
    return jsonify({"error": message}), status


@bp.errorhandler(404)
def _not_found(_):
    return jsonify({"error": "Not found."}), 404


@bp.errorhandler(500)
def _server_error(_):
    db.session.rollback()
    return jsonify({"error": "Something broke on our side."}), 500


# --------------------------------------------------------------- serializers
def agent_json(a):
    return {
        "id": a.id,
        "name": a.name,
        "provider": a.provider,
        "model": a.model,
        "role": a.role,
        "system_prompt": a.system_prompt,
        "temperature": a.temperature,
        "max_tokens": a.max_tokens,
        "color": a.color,
        "credential_id": a.credential_id,
        "has_key": a.credential_id is not None
        or bool(current_app.config.get("FALLBACK_KEYS", {}).get(a.provider)),
    }


def credential_json(c):
    return {
        "id": c.id,
        "provider": c.provider,
        "label": c.label,
        "hint": c.key_hint,
        "base_url": c.base_url,
        "created_at": c.created_at.isoformat() + "Z",
    }


def group_json(g, detail=False):
    data = {
        "id": g.id,
        "name": g.name,
        "purpose": g.purpose,
        "rounds": g.rounds,
        "synthesizer_agent_id": g.synthesizer_agent_id,
        "agent_count": len(g.members),
        "discussion_count": len(g.discussions),
    }
    if detail:
        data["members"] = [
            dict(agent_json(m.agent), seat=i + 1)
            for i, m in enumerate(g.members)
            if m.agent and not m.agent.archived
        ]
        data["discussions"] = [discussion_json(d) for d in g.discussions[:20]]
    return data


def discussion_json(d, with_messages=False):
    data = {
        "id": d.id,
        "group_id": d.group_id,
        "question": d.question,
        "mode": d.mode,
        "status": d.status,
        "stage": d.stage,
        "error": d.error,
        "created_at": d.created_at.isoformat() + "Z",
    }
    if with_messages:
        data["messages"] = [m.as_dict(html=render(m.content)) for m in d.messages]
        data["files"] = [f.as_dict() for f in d.attachments]
    return data


def owned_group(group_id):
    g = db.session.get(Group, group_id)
    if g is None or g.user_id != current_user.id:
        abort(404)
    return g


def owned_discussion(discussion_id):
    d = db.session.get(Discussion, discussion_id)
    if d is None or d.group.user_id != current_user.id:
        abort(404)
    return d


# --------------------------------------------------------------------- session
@bp.get("/session")
def get_session():
    """Called on every page load. Also mints the CSRF token."""
    payload = {
        "csrf_token": csrf_token(),
        "authenticated": current_user.is_authenticated,
        "providers": [{"key": k, "label": v} for k, v in PROVIDER_CHOICES],
        "suggested_models": SUGGESTED_MODELS,
        "limits": {
            "max_rounds": current_app.config["MAX_ROUNDS"],
            "max_agents_per_group": current_app.config["MAX_AGENTS_PER_GROUP"],
            "min_password_length": current_app.config.get("MIN_PASSWORD_LENGTH", 10),
        },
        "google_enabled": bool(
            current_app.config.get("GOOGLE_OAUTH", {}).get("enabled")
        ),
    }
    if current_user.is_authenticated:
        payload["user"] = {
            "id": current_user.id,
            "email": current_user.email,
            "display_name": current_user.display_name,
            "avatar_url": current_user.avatar_url,
            "has_password": current_user.has_password,
            "via_google": bool(current_user.google_sub),
        }
    return jsonify(payload)


@bp.post("/auth/register")
def register():
    from ..models import User

    data = body()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    name = (data.get("display_name") or "").strip()
    min_len = current_app.config.get("MIN_PASSWORD_LENGTH", 10)

    if not EMAIL_RE.match(email):
        return fail("That email address is not valid.")
    if len(password) < min_len:
        return fail(f"Use a password of at least {min_len} characters.")
    if User.query.filter_by(email=email).first():
        return fail("An account already exists for that email. Sign in instead.")

    user = User(email=email, display_name=name or email.split("@")[0])
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user, remember=True)
    return jsonify({"ok": True, "user": {"id": user.id, "email": user.email,
                                         "display_name": user.display_name}})


@bp.post("/auth/login")
def login():
    from ..models import User

    data = body()
    email = (data.get("email") or "").strip().lower()
    user = User.query.filter_by(email=email).first()
    if user is None or not user.check_password(data.get("password") or ""):
        return fail("Email or password is wrong.", 401)
    login_user(user, remember=True)
    return jsonify({"ok": True, "user": {"id": user.id, "email": user.email,
                                         "display_name": user.display_name}})


@bp.post("/auth/logout")
def logout():
    logout_user()
    session.clear()
    return jsonify({"ok": True})


# ----------------------------------------------------------------- credentials
@bp.get("/credentials")
@auth_required
def list_credentials():
    rows = (
        Credential.query.filter_by(user_id=current_user.id)
        .order_by(Credential.created_at.desc())
        .all()
    )
    return jsonify({"credentials": [credential_json(c) for c in rows]})


@bp.post("/credentials")
@auth_required
def create_credential():
    data = body()
    provider = data.get("provider", "")
    secret = (data.get("api_key") or "").strip()
    base_url = (data.get("base_url") or "").strip()

    if provider not in REGISTRY:
        return fail("Pick a provider.")
    if not secret:
        return fail("Paste the API key.")
    if REGISTRY[provider].needs_base_url and not base_url:
        return fail("A custom model needs a base URL, e.g. https://host/v1")

    try:
        cred = Credential(
            user_id=current_user.id,
            provider=provider,
            label=(data.get("label") or "").strip() or f"{REGISTRY[provider].label} key",
            key_encrypted=encrypt(secret),
            key_hint=mask(secret),
            base_url=base_url or None,
        )
    except EncryptionNotConfigured as exc:
        return fail(str(exc), 500)

    db.session.add(cred)
    db.session.commit()
    return jsonify({"credential": credential_json(cred)}), 201


@bp.delete("/credentials/<int:cred_id>")
@auth_required
def delete_credential(cred_id):
    cred = db.session.get(Credential, cred_id)
    if cred is None or cred.user_id != current_user.id:
        abort(404)
    db.session.delete(cred)
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------- agents
@bp.get("/agents")
@auth_required
def list_agents():
    rows = (
        Agent.query.filter_by(user_id=current_user.id, archived=False)
        .order_by(Agent.created_at)
        .all()
    )
    return jsonify({"agents": [agent_json(a) for a in rows]})


@bp.post("/agents")
@auth_required
def create_agent():
    data = body()
    provider = data.get("provider", "")
    name = (data.get("name") or "").strip()
    model = (data.get("model") or "").strip()

    if provider not in REGISTRY:
        return fail("Pick a provider.")
    if not name:
        return fail("Give the agent a name — the others address it by name.")
    if not model:
        return fail("Enter a model name.")

    credential = None
    if data.get("credential_id"):
        credential = db.session.get(Credential, int(data["credential_id"]))
        if credential is None or credential.user_id != current_user.id:
            abort(404)

    try:
        temperature = float(data.get("temperature", 0.7))
        max_tokens = int(data.get("max_tokens", 1200))
    except (TypeError, ValueError):
        return fail("Temperature and max tokens must be numbers.")

    count = Agent.query.filter_by(user_id=current_user.id).count()
    agent = Agent(
        user_id=current_user.id,
        credential_id=credential.id if credential else None,
        name=name[:60],
        provider=provider,
        model=model[:120],
        role=(data.get("role") or "").strip()[:120],
        system_prompt=(data.get("system_prompt") or "").strip(),
        temperature=min(max(temperature, 0.0), 2.0),
        max_tokens=min(max(max_tokens, 200), 8000),
        color=SEAT_COLORS[count % len(SEAT_COLORS)],
    )
    db.session.add(agent)
    db.session.commit()
    return jsonify({"agent": agent_json(agent)}), 201


@bp.delete("/agents/<int:agent_id>")
@auth_required
def delete_agent(agent_id):
    agent = db.session.get(Agent, agent_id)
    if agent is None or agent.user_id != current_user.id:
        abort(404)
    agent.archived = True
    db.session.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------- groups
@bp.get("/groups")
@auth_required
def list_groups():
    rows = (
        Group.query.filter_by(user_id=current_user.id)
        .order_by(Group.created_at.desc())
        .all()
    )
    return jsonify({"groups": [group_json(g) for g in rows]})


@bp.post("/groups")
@auth_required
def create_group():
    data = body()
    name = (data.get("name") or "").strip()
    if not name:
        return fail("Name the chat.")
    rounds = int(data.get("rounds") or 2)
    group = Group(
        user_id=current_user.id,
        name=name[:120],
        purpose=(data.get("purpose") or "").strip(),
        rounds=max(1, min(rounds, current_app.config["MAX_ROUNDS"])),
    )
    db.session.add(group)
    db.session.commit()
    return jsonify({"group": group_json(group, detail=True)}), 201


@bp.get("/groups/<int:group_id>")
@auth_required
def get_group(group_id):
    return jsonify({"group": group_json(owned_group(group_id), detail=True)})


@bp.patch("/groups/<int:group_id>")
@auth_required
def update_group(group_id):
    group = owned_group(group_id)
    data = body()
    if "name" in data and (data["name"] or "").strip():
        group.name = data["name"].strip()[:120]
    if "purpose" in data:
        group.purpose = (data["purpose"] or "").strip()
    if "rounds" in data:
        group.rounds = max(1, min(int(data["rounds"]), current_app.config["MAX_ROUNDS"]))
    if "synthesizer_agent_id" in data:
        chair = data["synthesizer_agent_id"]
        group.synthesizer_agent_id = int(chair) if chair else None
    db.session.commit()
    return jsonify({"group": group_json(group, detail=True)})


@bp.delete("/groups/<int:group_id>")
@auth_required
def delete_group(group_id):
    group = owned_group(group_id)
    db.session.delete(group)
    db.session.commit()
    return jsonify({"ok": True})


@bp.post("/groups/<int:group_id>/members")
@auth_required
def add_member(group_id):
    group = owned_group(group_id)
    agent = db.session.get(Agent, int(body().get("agent_id", 0)))
    if agent is None or agent.user_id != current_user.id:
        abort(404)

    limit = current_app.config["MAX_AGENTS_PER_GROUP"]
    if len(group.members) >= limit:
        return fail(
            f"A chat holds at most {limit} agents. Beyond that it gets "
            "slow and repetitive."
        )
    if any(m.agent_id == agent.id for m in group.members):
        return fail(f"{agent.name} is already in this chat.")

    highest = max((m.seat for m in group.members), default=-1)
    db.session.add(
        GroupMember(group_id=group.id, agent_id=agent.id, seat=highest + 1)
    )
    db.session.commit()
    return jsonify({"group": group_json(group, detail=True)})


@bp.delete("/groups/<int:group_id>/members/<int:agent_id>")
@auth_required
def remove_member(group_id, agent_id):
    group = owned_group(group_id)
    member = GroupMember.query.filter_by(group_id=group.id, agent_id=agent_id).first()
    if member:
        db.session.delete(member)
        db.session.commit()
        _renumber(group)
    return jsonify({"group": group_json(group, detail=True)})


def _renumber(group):
    """Keep seats as 0,1,2… with no gaps, so ordering stays predictable."""
    db.session.refresh(group)
    for index, member in enumerate(
        sorted(group.members, key=lambda m: m.seat)
    ):
        member.seat = index
    db.session.commit()


@bp.patch("/groups/<int:group_id>/members/order")
@auth_required
def reorder_members(group_id):
    """Set speaking order.

    Order is not cosmetic: seat 1 speaks first and answers only your message,
    while later seats also read the turns taken before them in the same pass.
    Whoever you put first frames the discussion.
    """
    group = owned_group(group_id)
    wanted = body().get("agent_ids")
    if not isinstance(wanted, list) or not wanted:
        return fail("Send the agent ids in the order you want them to speak.")

    try:
        wanted = [int(a) for a in wanted]
    except (TypeError, ValueError):
        return fail("Those agent ids are not valid.")

    members = {m.agent_id: m for m in group.members}
    if set(wanted) != set(members):
        return fail(
            "That order does not match who is in the chat. Reload the page and "
            "try again."
        )

    for index, agent_id in enumerate(wanted):
        members[agent_id].seat = index
    db.session.commit()
    return jsonify({"group": group_json(group, detail=True)})


# ----------------------------------------------------------------- discussions
def _post_human_message(discussion, text):
    """Record what the human said. Everything after it reads this."""
    exchange = (
        Message.query.filter_by(discussion_id=discussion.id, role="question").count()
        + 1
    )
    message = Message(
        discussion_id=discussion.id,
        role="question",
        round_no=exchange,
        speaker=current_user.display_name or "You",
        content=text,
        color="#172033",
    )
    db.session.add(message)
    db.session.commit()
    return message


def _responders(group, data):
    """Who replies to this message: everyone, one agent, or nobody."""
    seated = group.seated_agents
    reply_to = (data.get("reply") or "all").strip()

    if reply_to == "none":
        return [], False
    if reply_to.startswith("agent:"):
        try:
            wanted = int(reply_to.split(":", 1)[1])
        except ValueError:
            return seated, False
        picked = [a for a in seated if a.id == wanted]
        return picked, True
    return seated, False


@bp.post("/groups/<int:group_id>/ask")
@auth_required
def ask(group_id):
    """Open a new topic in this chat."""
    group = owned_group(group_id)
    data = body()
    question = (data.get("question") or "").strip()
    if not question:
        return fail("Type something to start the conversation.")
    if not group.seated_agents:
        return fail("Add at least one agent to this chat first.")

    mode = data.get("mode") if data.get("mode") in Discussion.MODES else "step"

    discussion = Discussion(
        group_id=group.id,
        question=question,
        rounds=1,
        mode=mode,
        status="queued",
        stage="Waiting for you",
    )
    db.session.add(discussion)
    db.session.commit()

    _post_human_message(discussion, question)

    if mode == "flow":
        start_flow(discussion.id)
    else:
        agents, direct = _responders(group, data)
        if agents:
            start_turns(discussion.id, [a.id for a in agents], direct=direct)
        else:
            discussion.status = "idle"
            db.session.commit()

    return jsonify({"discussion": discussion_json(discussion)}), 201


@bp.post("/discussions/<int:discussion_id>/say")
@auth_required
def say(discussion_id):
    """Add your message to an open room and let the agents answer it."""
    discussion = owned_discussion(discussion_id)
    data = body()
    text = (data.get("text") or "").strip()
    if not text:
        return fail("Type a message first.")

    # Interjecting into a running flow is the point of flow mode: the message
    # goes into the transcript and the next speaker answers it. No new thread.
    if discussion.status == "running":
        if discussion.mode != "flow":
            return fail("The room is still answering. Wait for it, or press Stop.")
        message = _post_human_message(discussion, text)
        return jsonify({
            "message": message.as_dict(html=render(message.content)),
            "replying": [],
            "interjected": True,
        })

    message = _post_human_message(discussion, text)

    if data.get("mode") == "flow" or (
        data.get("mode") is None and discussion.mode == "flow"
    ):
        discussion.mode = "flow"
        db.session.commit()
        start_flow(discussion.id)
        return jsonify({
            "message": message.as_dict(html=render(message.content)),
            "replying": [a.id for a in discussion.group.seated_agents],
        })

    agents, direct = _responders(discussion.group, data)
    if agents:
        start_turns(discussion.id, [a.id for a in agents], direct=direct)
    else:
        discussion.status = "idle"
        discussion.stage = "Waiting for you"
        db.session.commit()

    return jsonify({"message": message.as_dict(html=render(message.content)),
                    "replying": [a.id for a in agents]})


@bp.post("/discussions/<int:discussion_id>/continue")
@auth_required
def continue_room(discussion_id):
    """Let the room talk among itself again without you saying anything."""
    discussion = owned_discussion(discussion_id)
    if discussion.status == "running":
        return fail("The room is still answering.")

    data = body()
    mode = data.get("mode") if data.get("mode") in Discussion.MODES else discussion.mode

    if not discussion.group.seated_agents:
        return fail("Nobody is seated in this chat.")

    if mode == "flow":
        discussion.mode = "flow"
        db.session.commit()
        start_flow(discussion.id)
        return jsonify({
            "ok": True,
            "mode": "flow",
            "replying": [a.id for a in discussion.group.seated_agents],
        })

    agents, direct = _responders(discussion.group, data)
    if not agents:
        return fail("Nobody is seated in this chat.")
    discussion.mode = "step"
    db.session.commit()
    start_turns(
        discussion.id, [a.id for a in agents], direct=direct, nudge=not direct
    )
    return jsonify({"ok": True, "mode": "step", "replying": [a.id for a in agents]})


@bp.post("/discussions/<int:discussion_id>/retry/<int:message_id>")
@auth_required
def retry_turn(discussion_id, message_id):
    """Run one agent again, replacing a turn that failed."""
    discussion = owned_discussion(discussion_id)
    if discussion.status == "running":
        return fail("The room is still answering.")

    message = db.session.get(Message, message_id)
    if message is None or message.discussion_id != discussion.id:
        abort(404)
    if not message.agent_id:
        return fail("That message did not come from an agent.")

    agent = db.session.get(Agent, message.agent_id)
    if agent is None or agent.archived:
        return fail("That agent no longer exists. Add it back to the group first.")

    start_turns(discussion.id, [agent.id], replace_message_id=message.id, direct=True)
    return jsonify({"ok": True, "agent_id": agent.id})


@bp.get("/discussions/<int:discussion_id>")
@auth_required
def get_discussion(discussion_id):
    d = owned_discussion(discussion_id)
    return jsonify({"discussion": discussion_json(d, with_messages=True)})


@bp.get("/discussions/<int:discussion_id>/messages")
@auth_required
def discussion_messages(discussion_id):
    d = owned_discussion(discussion_id)
    after = request.args.get("after", 0, type=int)
    rows = (
        Message.query.filter(Message.discussion_id == d.id, Message.id > after)
        .order_by(Message.id)
        .all()
    )
    return jsonify(
        {
            "status": d.status,
            "stage": d.stage,
            "error": d.error,
            "messages": [m.as_dict(html=render(m.content)) for m in rows],
        }
    )


@bp.post("/discussions/<int:discussion_id>/cancel")
@auth_required
def cancel(discussion_id):
    d = owned_discussion(discussion_id)
    d.cancel_requested = True
    db.session.commit()
    return jsonify({"ok": True})


@bp.get("/discussions/<int:discussion_id>/stream")
@auth_required
def stream(discussion_id):
    """Reuses the SSE generator from the server-rendered API."""
    from .api import stream as legacy_stream

    return legacy_stream(discussion_id)


# ------------------------------------------------------------- Google sign-in
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def _site_url(fragment="/", error=None):
    """Where to send the browser back to after the OAuth round trip."""
    origin = current_app.config.get("PUBLIC_ORIGIN", "").rstrip("/")
    query = f"?auth_error={urlencode({'e': error})[2:]}" if error else ""
    return f"{origin}/{query}#{fragment}"


@bp.get("/auth/google/start")
def google_start():
    """Kick off the OAuth code flow. The browser navigates here directly."""
    cfg = current_app.config.get("GOOGLE_OAUTH", {})
    if not cfg.get("enabled"):
        return fail("Google sign-in is not configured on this server.", 404)

    state = secrets.token_urlsafe(24)
    session["google_state"] = state
    session["google_state_at"] = int(time.time())

    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@bp.get("/auth/google/callback")
def google_callback():
    """Google sends the user back here with a one-time code."""
    from ..models import User

    cfg = current_app.config.get("GOOGLE_OAUTH", {})
    if not cfg.get("enabled"):
        return redirect(_site_url("/signin", "Google sign-in is not configured."))

    if request.args.get("error"):
        return redirect(_site_url("/signin", "Google sign-in was cancelled."))

    # State check: without it, an attacker can complete a login in your browser.
    expected = session.pop("google_state", None)
    issued_at = session.pop("google_state_at", 0)
    got = request.args.get("state", "")
    if not expected or not got or not secrets.compare_digest(got, expected):
        return redirect(_site_url("/signin", "Sign-in expired. Try again."))
    if time.time() - issued_at > 600:
        return redirect(_site_url("/signin", "Sign-in took too long. Try again."))

    code = request.args.get("code", "")
    if not code:
        return redirect(_site_url("/signin", "Google did not return a code."))

    try:
        token_response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "redirect_uri": cfg["redirect_uri"],
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        if token_response.status_code >= 400:
            current_app.logger.warning(
                "Google token exchange failed: %s", token_response.text[:300]
            )
            return redirect(_site_url("/signin", "Google rejected the sign-in."))
        access_token = token_response.json().get("access_token")

        # Fetching userinfo over TLS avoids having to verify the ID token's
        # signature ourselves, which is where OAuth integrations usually break.
        info_response = requests.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20,
        )
        if info_response.status_code >= 400:
            return redirect(_site_url("/signin", "Could not read your Google profile."))
        info = info_response.json()
    except requests.RequestException as exc:
        current_app.logger.warning("Google sign-in network error: %s", exc)
        return redirect(_site_url("/signin", "Could not reach Google. Try again."))

    sub = info.get("sub")
    email = (info.get("email") or "").strip().lower()
    if not sub or not email:
        return redirect(_site_url("/signin", "Google did not share an email address."))
    if not info.get("email_verified", True):
        return redirect(_site_url("/signin", "That Google email is not verified."))

    allowed = cfg.get("allowed_domains") or []
    if allowed and email.rsplit("@", 1)[-1] not in allowed:
        return redirect(
            _site_url("/signin", "That domain is not allowed on this server.")
        )

    user = User.query.filter_by(google_sub=sub).first()
    if user is None:
        # Same email, signed up with a password earlier: link the two rather
        # than creating a second account they cannot find their groups in.
        user = User.query.filter_by(email=email).first()
        if user is not None:
            user.google_sub = sub
        else:
            user = User(
                email=email,
                display_name=(info.get("name") or email.split("@")[0])[:80],
                google_sub=sub,
                password_hash=None,
            )
            db.session.add(user)

    user.avatar_url = (info.get("picture") or "")[:512]
    if not user.display_name:
        user.display_name = (info.get("name") or email.split("@")[0])[:80]
    from datetime import datetime

    user.last_login_at = datetime.utcnow()
    db.session.commit()

    login_user(user, remember=True)
    session["csrf_token"] = secrets.token_urlsafe(32)
    return redirect(_site_url("/"))


# ------------------------------------------------------------- model listings
# Vendors are asked at most once every few minutes per key; the answer rarely
# changes and the picker should not add a round trip to every page view.
_MODEL_CACHE = {}
_MODEL_CACHE_TTL = 300


@bp.get("/models")
@auth_required
def list_models():
    """Real models the given key can use, with the static list as a fallback."""
    provider = request.args.get("provider", "")
    credential_id = request.args.get("credential_id", type=int)

    if provider not in REGISTRY:
        return fail("Unknown provider.")

    suggested = [
        {"id": m, "label": m} for m in (SUGGESTED_MODELS.get(provider) or [])
    ]

    api_key = ""
    base_url = ""
    if credential_id:
        cred = db.session.get(Credential, credential_id)
        if cred is None or cred.user_id != current_user.id:
            abort(404)
        if cred.provider != provider:
            return fail("That key belongs to a different provider.")
        from ..crypto import decrypt

        try:
            api_key = decrypt(cred.key_encrypted)
        except ValueError as exc:
            return jsonify({"models": suggested, "source": "suggested",
                            "note": str(exc)})
        base_url = cred.base_url or ""
    else:
        api_key = current_app.config.get("FALLBACK_KEYS", {}).get(provider, "")

    if not api_key:
        return jsonify({
            "models": suggested,
            "source": "suggested",
            "note": "Add an API key to see the models your account can actually use.",
        })

    cache_key = (provider, credential_id or 0, base_url)
    cached = _MODEL_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _MODEL_CACHE_TTL and not request.args.get("refresh"):
        return jsonify({"models": cached[1], "source": "live", "cached": True})

    try:
        client = get_provider(provider, api_key, "", base_url,
                              current_app.config["PROVIDER_TIMEOUT"])
        models = client.list_models()
    except NotImplementedError:
        return jsonify({"models": suggested, "source": "suggested"})
    except ProviderError as exc:
        return jsonify({
            "models": suggested,
            "source": "suggested",
            "note": str(exc),
        })

    if not models:
        return jsonify({
            "models": suggested,
            "source": "suggested",
            "note": f"{provider} returned no usable models for this key.",
        })

    _MODEL_CACHE[cache_key] = (time.time(), models)
    return jsonify({"models": models, "source": "live"})


# ------------------------------------------------------------------- files
def _upload_dir():
    path = current_app.config.get("UPLOAD_DIR") or "uploads"
    os.makedirs(path, exist_ok=True)
    return path


@bp.get("/discussions/<int:discussion_id>/files")
@auth_required
def list_files(discussion_id):
    discussion = owned_discussion(discussion_id)
    return jsonify({"files": [f.as_dict() for f in discussion.attachments]})


@bp.post("/groups/<int:group_id>/files")
@auth_required
def upload_file(group_id):
    """Share a file into a topic. Everyone in the chat reads it.

    Takes a topic id when there is one, and opens a topic when there is not,
    so the paperclip works before you have typed anything.
    """
    group = owned_group(group_id)

    discussion_id = request.form.get("discussion_id", type=int)
    if discussion_id:
        discussion = db.session.get(Discussion, discussion_id)
        if discussion is None or discussion.group_id != group.id:
            abort(404)
    else:
        discussion = Discussion(
            group_id=group.id,
            question="Shared files",
            rounds=1,
            mode="step",
            status="idle",
            stage="Waiting for you",
        )
        db.session.add(discussion)
        db.session.commit()

    uploaded = request.files.getlist("file")
    if not uploaded:
        return fail("No file came through. Pick one and try again.")

    limit = current_app.config.get("MAX_FILES_PER_TOPIC", 10)
    if len(discussion.attachments) + len(uploaded) > limit:
        return fail(
            f"A topic holds at most {limit} files. Start a new topic, or "
            "remove one first."
        )

    saved = []
    for item in uploaded:
        if not item or not item.filename:
            continue
        data = item.read()
        if not data:
            return fail(f"{item.filename} is empty.")

        name = safe_name(item.filename)
        text, truncated, note = extract(
            name, data, current_app.config.get("MAX_FILE_CHARS", 20000)
        )

        stored_path = ""
        try:
            unique = f"{discussion.id}-{secrets.token_hex(6)}-{name}"
            stored_path = os.path.join(_upload_dir(), unique)
            with open(stored_path, "wb") as handle:
                handle.write(data)
        except OSError as exc:
            # Keeping the original is a convenience; failing to keep it must
            # not stop the agents from reading the text we already extracted.
            current_app.logger.warning("Could not store %s: %s", name, exc)
            stored_path = ""

        attachment = Attachment(
            discussion_id=discussion.id,
            user_id=current_user.id,
            filename=item.filename[:255],
            mime=(item.mimetype or "")[:120],
            size_bytes=len(data),
            stored_path=stored_path,
            extracted=text,
            extract_chars=len(text),
            truncated=truncated,
            note=note[:255],
        )
        db.session.add(attachment)
        db.session.commit()
        saved.append(attachment)

        # The room should see that a file arrived, in order, like any message.
        summary = (
            f"{current_user.display_name or 'You'} shared {attachment.filename}"
            + (f" ({attachment.extract_chars:,} characters read)" if text
               else f" — {note}")
            + (" — truncated to fit" if truncated else "")
        )
        db.session.add(
            Message(
                discussion_id=discussion.id,
                role="note",
                round_no=max(
                    1,
                    Message.query.filter_by(
                        discussion_id=discussion.id, role="question"
                    ).count(),
                ),
                speaker="Room",
                content=summary,
                color="#8b96a3",
            )
        )
        db.session.commit()

    return jsonify({
        "discussion_id": discussion.id,
        "files": [f.as_dict() for f in saved],
    }), 201


@bp.delete("/files/<int:file_id>")
@auth_required
def delete_file(file_id):
    attachment = db.session.get(Attachment, file_id)
    if attachment is None or attachment.discussion.group.user_id != current_user.id:
        abort(404)
    if attachment.stored_path:
        try:
            os.remove(attachment.stored_path)
        except OSError:
            pass
    db.session.delete(attachment)
    db.session.commit()
    return jsonify({"ok": True})


@bp.get("/files/<int:file_id>/download")
@auth_required
def download_file(file_id):
    attachment = db.session.get(Attachment, file_id)
    if attachment is None or attachment.discussion.group.user_id != current_user.id:
        abort(404)
    if not attachment.stored_path or not os.path.isfile(attachment.stored_path):
        return fail("That file is no longer on disk.", 410)
    return send_file(
        attachment.stored_path,
        as_attachment=True,
        download_name=attachment.filename,
    )
