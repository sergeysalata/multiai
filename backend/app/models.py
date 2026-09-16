from datetime import datetime

from flask_login import UserMixin
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow():
    return datetime.utcnow()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    display_name = db.Column(db.String(80), nullable=False, default="")
    # Null for accounts that only ever signed in with Google.
    password_hash = db.Column(db.String(255), nullable=True)
    google_sub = db.Column(db.String(64), unique=True, nullable=True, index=True)
    avatar_url = db.Column(db.String(512), nullable=False, default="")
    # Put into every agent's system prompt, so the room knows who it is
    # talking to instead of pitching every answer at an unknown reader.
    about = db.Column(db.Text, nullable=False, default="")
    locale = db.Column(db.String(32), nullable=False, default="")
    last_login_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    credentials = db.relationship(
        "Credential", back_populates="user", cascade="all, delete-orphan"
    )
    agents = db.relationship(
        "Agent", back_populates="user", cascade="all, delete-orphan"
    )
    groups = db.relationship(
        "Group", back_populates="user", cascade="all, delete-orphan"
    )

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw):
        if not self.password_hash:
            return False
        return check_password_hash(self.password_hash, raw)

    @property
    def has_password(self):
        return bool(self.password_hash)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Credential(db.Model):
    """An API key for one provider, encrypted at rest."""

    __tablename__ = "credentials"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider = db.Column(db.String(32), nullable=False)
    label = db.Column(db.String(80), nullable=False)
    key_encrypted = db.Column(db.Text, nullable=False)
    key_hint = db.Column(db.String(64), nullable=False, default="")
    base_url = db.Column(db.String(255), nullable=True)  # custom / self-hosted
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="credentials")
    agents = db.relationship("Agent", back_populates="credential")


class Agent(db.Model):
    """A configured participant: provider + model + persona."""

    __tablename__ = "agents"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    credential_id = db.Column(
        db.Integer, db.ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True
    )

    name = db.Column(db.String(60), nullable=False)
    provider = db.Column(db.String(32), nullable=False)
    model = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120), nullable=False, default="")
    system_prompt = db.Column(db.Text, nullable=False, default="")
    temperature = db.Column(db.Float, nullable=False, default=0.7)
    max_tokens = db.Column(db.Integer, nullable=False, default=4000)
    color = db.Column(db.String(16), nullable=False, default="#2F2BA8")
    # Either an uploaded image, or one of the patterns the browser draws from
    # `color`. The preset costs no storage and no request.
    avatar_path = db.Column(db.String(512), nullable=False, default="")
    avatar_mime = db.Column(db.String(60), nullable=False, default="")
    avatar_preset = db.Column(db.String(32), nullable=False, default="monogram")
    # The vendor's own server-side search. Off by default: an agent that can
    # search and others that cannot is not a fair room.
    web_search = db.Column(db.Boolean, nullable=False, default=False)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="agents")
    credential = db.relationship("Credential", back_populates="agents")
    memberships = db.relationship(
        "GroupMember", back_populates="agent", cascade="all, delete-orphan"
    )

    AVATAR_PRESETS = ("monogram", "rings", "bars", "grid", "dots", "chevron")

    @property
    def has_avatar(self):
        return bool(self.avatar_path)

    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "role": self.role,
            "color": self.color,
            "avatar_preset": self.avatar_preset,
            "has_avatar": self.has_avatar,
            "web_search": self.web_search,
        }


class Group(db.Model):
    """A room of agents that deliberates on questions."""

    __tablename__ = "groups"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name = db.Column(db.String(120), nullable=False)
    purpose = db.Column(db.Text, nullable=False, default="")
    rounds = db.Column(db.Integer, nullable=False, default=2)
    # Stop a free-running conversation when replies get short. A guess, and
    # occasionally a wrong one, so it is switchable per chat.
    auto_stop = db.Column(db.Boolean, nullable=False, default=True)
    # "brief" | "normal" | "full" — an instruction, not a token ceiling.
    reply_style = db.Column(db.String(16), nullable=False, default="normal")
    # Flow turns per run before it pauses. 0 means no cap.
    flow_turn_limit = db.Column(db.Integer, nullable=False, default=40)
    # Replaces the built-in behaviour rules for this chat. Empty = default.
    room_rules = db.Column(db.Text, nullable=False, default="")
    # Which template this chat was made from, for the settings page.
    template_key = db.Column(db.String(40), nullable=False, default="")
    synthesizer_agent_id = db.Column(
        db.Integer, db.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    user = db.relationship("User", back_populates="groups")
    members = db.relationship(
        "GroupMember",
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="GroupMember.seat",
    )
    discussions = db.relationship(
        "Discussion",
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="Discussion.id.desc()",
    )
    synthesizer = db.relationship("Agent", foreign_keys=[synthesizer_agent_id])

    @property
    def seated_agents(self):
        return [m.agent for m in self.members if m.agent and not m.agent.archived]


class GroupMember(db.Model):
    __tablename__ = "group_members"
    __table_args__ = (UniqueConstraint("group_id", "agent_id", name="uq_group_agent"),)

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(
        db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    agent_id = db.Column(
        db.Integer, db.ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    seat = db.Column(db.Integer, nullable=False, default=0)

    group = db.relationship("Group", back_populates="members")
    agent = db.relationship("Agent", back_populates="memberships")


class RoomTemplate(db.Model):
    """A room's character: behaviour rules plus the settings that suit them."""

    __tablename__ = "room_templates"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    name = db.Column(db.String(80), nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    room_rules = db.Column(db.Text, nullable=False)
    reply_style = db.Column(db.String(16), nullable=False, default="normal")
    auto_stop = db.Column(db.Boolean, nullable=False, default=True)
    flow_turn_limit = db.Column(db.Integer, nullable=False, default=40)
    is_builtin = db.Column(db.Boolean, nullable=False, default=False)
    sort_order = db.Column(db.Integer, nullable=False, default=100)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    def as_dict(self):
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "room_rules": self.room_rules,
            "reply_style": self.reply_style,
            "auto_stop": self.auto_stop,
            "flow_turn_limit": self.flow_turn_limit,
            "is_builtin": self.is_builtin,
        }


class Discussion(db.Model):
    __tablename__ = "discussions"

    STATUSES = ("queued", "running", "idle", "done", "failed", "cancelled")
    MODES = ("step", "flow")

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(
        db.Integer, db.ForeignKey("groups.id", ondelete="CASCADE"), nullable=False
    )
    question = db.Column(db.Text, nullable=False)
    rounds = db.Column(db.Integer, nullable=False, default=1)  # unused since v3
    # "step": one pass, then wait for the human.
    # "flow": agents keep talking to each other until stopped.
    mode = db.Column(db.String(8), nullable=False, default="step")
    status = db.Column(db.String(16), nullable=False, default="queued", index=True)
    stage = db.Column(db.String(160), nullable=False, default="Waiting to start")
    error = db.Column(db.Text, nullable=True)
    cancel_requested = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    finished_at = db.Column(db.DateTime, nullable=True)

    group = db.relationship("Group", back_populates="discussions")
    messages = db.relationship(
        "Message",
        back_populates="discussion",
        cascade="all, delete-orphan",
        order_by="Message.id",
    )
    attachments = db.relationship(
        "Attachment",
        back_populates="discussion",
        cascade="all, delete-orphan",
        order_by="Attachment.id",
    )

    @property
    def verdict(self):
        for m in reversed(self.messages):
            if m.role == "verdict":
                return m
        return None


class Attachment(db.Model):
    """A file shared into a topic. `extracted` is what the agents read."""

    __tablename__ = "attachments"

    id = db.Column(db.Integer, primary_key=True)
    discussion_id = db.Column(
        db.Integer,
        db.ForeignKey("discussions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    filename = db.Column(db.String(255), nullable=False)
    mime = db.Column(db.String(120), nullable=False, default="")
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    stored_path = db.Column(db.String(512), nullable=False, default="")
    extracted = db.Column(db.Text, nullable=False, default="")
    extract_chars = db.Column(db.Integer, nullable=False, default=0)
    truncated = db.Column(db.Boolean, nullable=False, default=False)
    note = db.Column(db.String(255), nullable=False, default="")
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    discussion = db.relationship("Discussion", back_populates="attachments")

    @property
    def readable(self):
        return bool(self.extracted)

    def as_dict(self):
        return {
            "id": self.id,
            "filename": self.filename,
            "mime": self.mime,
            "size_bytes": self.size_bytes,
            "chars": self.extract_chars,
            "truncated": self.truncated,
            "readable": self.readable,
            "note": self.note,
            "created_at": self.created_at.isoformat() + "Z",
        }


class Message(db.Model):
    """One turn. Agent turns are fed back in as input to every later turn."""

    __tablename__ = "messages"

    ROLES = ("question", "turn", "verdict", "error", "note")

    id = db.Column(db.Integer, primary_key=True)
    discussion_id = db.Column(
        db.Integer,
        db.ForeignKey("discussions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id = db.Column(
        db.Integer, db.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True
    )
    role = db.Column(db.String(16), nullable=False, default="turn")
    round_no = db.Column(db.Integer, nullable=False, default=0)
    seat = db.Column(db.Integer, nullable=False, default=0)
    speaker = db.Column(db.String(60), nullable=False, default="You")
    provider = db.Column(db.String(32), nullable=False, default="")
    model = db.Column(db.String(120), nullable=False, default="")
    color = db.Column(db.String(16), nullable=False, default="#172033")
    content = db.Column(db.Text, nullable=False, default="")
    latency_ms = db.Column(db.Integer, nullable=True)
    input_chars = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    discussion = db.relationship("Discussion", back_populates="messages")

    def as_dict(self, html=None):
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "role": self.role,
            "round": self.round_no,
            "speaker": self.speaker,
            "provider": self.provider,
            "model": self.model,
            "color": self.color,
            "content": self.content,
            "html": html,
            "latency_ms": self.latency_ms,
            "input_chars": self.input_chars,
            "created_at": self.created_at.isoformat() + "Z",
        }
