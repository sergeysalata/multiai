import re

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user

from ..extensions import db
from ..models import User

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("display_name", "").strip()
        password = request.form.get("password", "")

        min_len = current_app.config.get("MIN_PASSWORD_LENGTH", 10)
        if not EMAIL_RE.match(email):
            flash("That email address is not valid.", "error")
        elif len(password) < min_len:
            flash(f"Use a password of at least {min_len} characters.", "error")
        elif User.query.filter_by(email=email).first():
            flash("An account already exists for that email. Sign in instead.", "error")
        else:
            user = User(email=email, display_name=name or email.split("@")[0])
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            return redirect(url_for("main.dashboard"))

    return render_template("auth.html", mode="register")


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user is None or not user.check_password(password):
            flash("Email or password is wrong.", "error")
        else:
            login_user(user, remember=True)
            nxt = request.args.get("next", "")
            if nxt.startswith("/"):
                return redirect(nxt)
            return redirect(url_for("main.dashboard"))

    return render_template("auth.html", mode="login")


@bp.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("main.landing"))
