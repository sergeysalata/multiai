import json
import time

from flask import Blueprint, Response, abort, current_app, jsonify, request, stream_with_context
from flask_login import current_user, login_required

from ..extensions import db
from ..models import Discussion, Group, Message
from ..orchestrator import start_discussion  # everyone speaks once
from ..rendering import render

# No url_prefix here: the whole app is mounted under server.url_prefix
# ("/api") by PrefixMiddleware, so these routes are already reachable at
# https://multiai.online/api/discussions/... Adding a prefix here would
# double it.
bp = Blueprint("api", __name__)

POLL_SECONDS = 0.8
MAX_STREAM_SECONDS = 30 * 60


def owned_discussion(discussion_id):
    d = db.session.get(Discussion, discussion_id)
    if d is None or d.group.user_id != current_user.id:
        abort(404)
    return d


@bp.post("/groups/<int:group_id>/ask")
@login_required
def ask(group_id):
    group = db.session.get(Group, group_id)
    if group is None or group.user_id != current_user.id:
        abort(404)

    payload = request.get_json(silent=True) or request.form
    question = (payload.get("question") or "").strip()
    if not question:
        return jsonify({"error": "Type the problem you want the panel to solve."}), 400
    if not group.seated_agents:
        return jsonify({"error": "Add at least one agent to this group first."}), 400

    rounds = int(payload.get("rounds") or group.rounds)
    rounds = max(1, min(rounds, current_app.config["MAX_ROUNDS"]))

    discussion = Discussion(
        group_id=group.id, question=question, rounds=rounds, status="queued"
    )
    db.session.add(discussion)
    db.session.commit()

    db.session.add(
        Message(
            discussion_id=discussion.id,
            role="question",
            round_no=0,
            speaker=current_user.display_name or "You",
            content=question,
            color="#172033",
        )
    )
    db.session.commit()

    start_discussion(discussion.id)
    return jsonify({"discussion_id": discussion.id})


@bp.post("/discussions/<int:discussion_id>/cancel")
@login_required
def cancel(discussion_id):
    d = owned_discussion(discussion_id)
    d.cancel_requested = True
    db.session.commit()
    return jsonify({"ok": True})


@bp.get("/discussions/<int:discussion_id>/messages")
@login_required
def messages(discussion_id):
    d = owned_discussion(discussion_id)
    after = request.args.get("after", 0, type=int)
    rows = (
        Message.query.filter(
            Message.discussion_id == d.id, Message.id > after
        )
        .order_by(Message.id)
        .all()
    )
    return jsonify(
        {
            "status": d.status,
            "stage": d.stage,
            "error": d.error,
            "rounds": d.rounds,
            "messages": [m.as_dict(html=render(m.content)) for m in rows],
        }
    )


@bp.get("/discussions/<int:discussion_id>/stream")
@login_required
def stream(discussion_id):
    """Server-sent events. Turns are pushed as they are committed, so the
    user watches the models answer each other in real time."""
    d = owned_discussion(discussion_id)
    last_id = request.args.get("after", 0, type=int)
    app = current_app._get_current_object()

    @stream_with_context
    def events():
        cursor = last_id
        last_stage = None
        started = time.monotonic()

        while True:
            with app.app_context():
                disc = db.session.get(Discussion, discussion_id)
                if disc is None:
                    break
                db.session.refresh(disc)
                rows = (
                    Message.query.filter(
                        Message.discussion_id == disc.id, Message.id > cursor
                    )
                    .order_by(Message.id)
                    .all()
                )
                for m in rows:
                    cursor = m.id
                    yield _event("message", m.as_dict(html=render(m.content)))

                if disc.stage != last_stage:
                    last_stage = disc.stage
                    yield _event("stage", {"stage": disc.stage, "status": disc.status})

                if disc.status in ("idle", "done", "failed", "cancelled"):
                    yield _event(
                        "end", {"status": disc.status, "error": disc.error}
                    )
                    break

            if time.monotonic() - started > MAX_STREAM_SECONDS:
                yield _event("end", {"status": "timeout", "error": None})
                break

            yield ": keep-alive\n\n"
            time.sleep(POLL_SECONDS)

    return Response(
        events(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # nginx: do not buffer SSE
            "Connection": "keep-alive",
        },
    )


def _event(name, data):
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"
