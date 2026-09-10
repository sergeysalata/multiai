from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from ..crypto import EncryptionNotConfigured, encrypt, mask
from ..extensions import db
from ..models import Agent, Credential, Discussion, Group, GroupMember
from ..providers import PROVIDER_CHOICES, REGISTRY, SUGGESTED_MODELS

bp = Blueprint("main", __name__)

SEAT_COLORS = [
    "#2F2BA8",  # indigo
    "#0E7C6B",  # teal
    "#A2452F",  # rust
    "#6B3FA0",  # violet
    "#B0761A",  # amber
    "#1F5FA8",  # blue
]

STARTER_AGENTS = [
    ("Claude", "anthropic", "claude-sonnet-4-5", "Systems thinker",
     "Look for second-order effects and failure modes the others miss."),
    ("ChatGPT", "openai", "gpt-4o", "Pragmatist",
     "Push for the concrete, shippable option. Ask what it costs."),
    ("Gemini", "gemini", "gemini-2.0-flash", "Researcher",
     "Bring evidence, comparisons and prior art. Flag unsupported claims."),
]


def owned_group(group_id):
    group = db.session.get(Group, group_id)
    if group is None or group.user_id != current_user.id:
        abort(404)
    return group


@bp.get("/")
def landing():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    return render_template("landing.html")


@bp.get("/dashboard")
@login_required
def dashboard():
    groups = (
        Group.query.filter_by(user_id=current_user.id)
        .order_by(Group.created_at.desc())
        .all()
    )
    agents = (
        Agent.query.filter_by(user_id=current_user.id, archived=False)
        .order_by(Agent.created_at)
        .all()
    )
    recent = (
        Discussion.query.join(Group)
        .filter(Group.user_id == current_user.id)
        .order_by(Discussion.id.desc())
        .limit(8)
        .all()
    )
    return render_template(
        "dashboard.html", groups=groups, agents=agents, recent=recent
    )


# ---------------------------------------------------------------- keys ----
@bp.route("/keys", methods=["GET", "POST"])
@login_required
def keys():
    if request.method == "POST":
        provider = request.form.get("provider", "")
        label = request.form.get("label", "").strip()
        secret = request.form.get("api_key", "").strip()
        base_url = request.form.get("base_url", "").strip()

        if provider not in REGISTRY:
            flash("Pick a provider.", "error")
        elif not secret:
            flash("Paste the API key.", "error")
        elif REGISTRY[provider].needs_base_url and not base_url:
            flash("A custom model needs a base URL, e.g. https://host/v1", "error")
        else:
            try:
                cred = Credential(
                    user_id=current_user.id,
                    provider=provider,
                    label=label or f"{REGISTRY[provider].label} key",
                    key_encrypted=encrypt(secret),
                    key_hint=mask(secret),
                    base_url=base_url or None,
                )
            except EncryptionNotConfigured as exc:
                flash(str(exc), "error")
            else:
                db.session.add(cred)
                db.session.commit()
                flash("Key saved and encrypted.", "ok")
                return redirect(url_for("main.keys"))

    creds = (
        Credential.query.filter_by(user_id=current_user.id)
        .order_by(Credential.created_at.desc())
        .all()
    )
    return render_template("keys.html", creds=creds, providers=PROVIDER_CHOICES,
                           registry=REGISTRY)


@bp.post("/keys/<int:cred_id>/delete")
@login_required
def delete_key(cred_id):
    cred = db.session.get(Credential, cred_id)
    if cred is None or cred.user_id != current_user.id:
        abort(404)
    db.session.delete(cred)
    db.session.commit()
    flash("Key deleted. Agents using it will need a new one.", "ok")
    return redirect(url_for("main.keys"))


# -------------------------------------------------------------- agents ----
@bp.route("/agents", methods=["GET", "POST"])
@login_required
def agents():
    if request.method == "POST":
        provider = request.form.get("provider", "")
        name = request.form.get("name", "").strip()
        model = request.form.get("model", "").strip()
        cred_id = request.form.get("credential_id", "")

        if provider not in REGISTRY:
            flash("Pick a provider.", "error")
        elif not name:
            flash("Give the agent a name — the others will address it by name.",
                  "error")
        elif not model:
            flash("Enter a model name.", "error")
        else:
            credential = None
            if cred_id:
                credential = db.session.get(Credential, int(cred_id))
                if credential is None or credential.user_id != current_user.id:
                    abort(404)
            count = Agent.query.filter_by(user_id=current_user.id).count()
            agent = Agent(
                user_id=current_user.id,
                credential_id=credential.id if credential else None,
                name=name,
                provider=provider,
                model=model,
                role=request.form.get("role", "").strip(),
                system_prompt=request.form.get("system_prompt", "").strip(),
                temperature=float(request.form.get("temperature") or 0.7),
                max_tokens=int(request.form.get("max_tokens") or 1200),
                color=SEAT_COLORS[count % len(SEAT_COLORS)],
            )
            db.session.add(agent)
            db.session.commit()
            flash(f"{agent.name} is ready to be added to a group.", "ok")
            return redirect(url_for("main.agents"))

    my_agents = (
        Agent.query.filter_by(user_id=current_user.id, archived=False)
        .order_by(Agent.created_at)
        .all()
    )
    creds = Credential.query.filter_by(user_id=current_user.id).all()
    return render_template(
        "agents.html",
        agents=my_agents,
        creds=creds,
        providers=PROVIDER_CHOICES,
        suggested=SUGGESTED_MODELS,
        registry=REGISTRY,
        starters=STARTER_AGENTS,
    )


@bp.post("/agents/<int:agent_id>/delete")
@login_required
def delete_agent(agent_id):
    agent = db.session.get(Agent, agent_id)
    if agent is None or agent.user_id != current_user.id:
        abort(404)
    agent.archived = True
    db.session.commit()
    flash(f"{agent.name} removed.", "ok")
    return redirect(url_for("main.agents"))


# -------------------------------------------------------------- groups ----
@bp.post("/groups")
@login_required
def create_group():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Name the group.", "error")
        return redirect(url_for("main.dashboard"))

    group = Group(
        user_id=current_user.id,
        name=name,
        purpose=request.form.get("purpose", "").strip(),
        rounds=max(1, min(int(request.form.get("rounds") or 2),
                          current_app.config["MAX_ROUNDS"])),
    )
    db.session.add(group)
    db.session.commit()
    return redirect(url_for("main.group", group_id=group.id))


@bp.get("/groups/<int:group_id>")
@login_required
def group(group_id):
    group = owned_group(group_id)
    available = (
        Agent.query.filter_by(user_id=current_user.id, archived=False)
        .order_by(Agent.created_at)
        .all()
    )
    seated_ids = {m.agent_id for m in group.members}
    discussion = None
    discussion_id = request.args.get("discussion", type=int)
    if discussion_id:
        discussion = db.session.get(Discussion, discussion_id)
        if discussion is None or discussion.group_id != group.id:
            abort(404)
    elif group.discussions:
        discussion = group.discussions[0]

    return render_template(
        "group.html",
        group=group,
        available=available,
        seated_ids=seated_ids,
        discussion=discussion,
        max_rounds=current_app.config["MAX_ROUNDS"],
        max_agents=current_app.config["MAX_AGENTS_PER_GROUP"],
    )


@bp.post("/groups/<int:group_id>/settings")
@login_required
def group_settings(group_id):
    group = owned_group(group_id)
    group.name = request.form.get("name", group.name).strip() or group.name
    group.purpose = request.form.get("purpose", "").strip()
    group.rounds = max(
        1, min(int(request.form.get("rounds") or group.rounds),
               current_app.config["MAX_ROUNDS"])
    )
    chair = request.form.get("synthesizer_agent_id", "")
    group.synthesizer_agent_id = int(chair) if chair else None
    db.session.commit()
    flash("Group updated.", "ok")
    return redirect(url_for("main.group", group_id=group.id))


@bp.post("/groups/<int:group_id>/members")
@login_required
def add_member(group_id):
    group = owned_group(group_id)
    agent_id = int(request.form.get("agent_id", 0))
    agent = db.session.get(Agent, agent_id)
    if agent is None or agent.user_id != current_user.id:
        abort(404)

    if len(group.members) >= current_app.config["MAX_AGENTS_PER_GROUP"]:
        flash(
            f"A group holds at most {current_app.config['MAX_AGENTS_PER_GROUP']} "
            "agents. More than that and the debate gets slow and repetitive.",
            "error",
        )
    elif any(m.agent_id == agent.id for m in group.members):
        flash(f"{agent.name} is already in this group.", "error")
    else:
        db.session.add(
            GroupMember(group_id=group.id, agent_id=agent.id, seat=len(group.members))
        )
        db.session.commit()
    return redirect(url_for("main.group", group_id=group.id))


@bp.post("/groups/<int:group_id>/members/<int:agent_id>/remove")
@login_required
def remove_member(group_id, agent_id):
    group = owned_group(group_id)
    member = GroupMember.query.filter_by(group_id=group.id, agent_id=agent_id).first()
    if member:
        db.session.delete(member)
        db.session.commit()
    return redirect(url_for("main.group", group_id=group.id))


@bp.post("/groups/<int:group_id>/delete")
@login_required
def delete_group(group_id):
    group = owned_group(group_id)
    db.session.delete(group)
    db.session.commit()
    flash("Group deleted.", "ok")
    return redirect(url_for("main.dashboard"))
