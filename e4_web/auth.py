"""E4: login session, role guard and CSRF protection."""
import hmac
import secrets
import time
from functools import wraps

from flask import abort, g, jsonify, redirect, request, session, url_for

_FAILS = {}            # ip -> (count, first_failure_time): tiny brute-force limiter


def wants_json():
    return request.is_json or request.accept_mimetypes.best_match(["text/html", "application/json"]) == "application/json"


def current_user():
    if "user" not in g:
        uid = session.get("user_id")
        g.user = g.repo.get_user(uid) if uid else None
    return g.user


def login_required(view):
    @wraps(view)
    def wrapper(*a, **kw):
        if current_user() is None:
            if wants_json():
                return jsonify(error="login required"), 401
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)
    return wrapper


def admin_required(view):
    """Role check on the SERVER for every admin route (FR19, FR20): hiding buttons is not access control."""
    @wraps(view)
    @login_required
    def wrapper(*a, **kw):
        if current_user()["role"] != "admin":
            if wants_json():
                return jsonify(error="admin only"), 403
            abort(403)
        return view(*a, **kw)
    return wrapper


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def check_csrf():
    """Form posts need the hidden token. JSON posts are protected by the content type
    (a cross-site page cannot send application/json without a CORS preflight, which we never allow)."""
    if request.method not in ("POST", "PUT", "DELETE") or request.is_json:
        return
    sent = request.form.get("csrf_token", "")
    if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
        abort(400, "missing or invalid form token; reload the page and try again")


def too_many_failures(ip):
    c, t = _FAILS.get(ip, (0, 0))
    if time.time() - t > 60:
        return False
    return c >= 5


def note_failure(ip):
    c, t = _FAILS.get(ip, (0, 0))
    _FAILS[ip] = (1, time.time()) if time.time() - t > 60 else (c + 1, t)


def clear_failures(ip):
    _FAILS.pop(ip, None)
