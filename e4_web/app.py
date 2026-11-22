"""E4 Submission & Status Interface Engine: the web server.

Run as its own process (started by the supervisor):  python -m e4_web.app
Talks only to E2 (through Repository). It never decides which GPU a job gets.
"""
import os
import secrets
import time

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)

import config
from e2_persistence import GpuBusy, NotFound, Repository

from . import auth
from .validation import validate_gpu, validate_job

PUBLIC_JOB_FIELDS = ("job_id", "state", "template", "memory_gb", "duration_min", "priority", "deadline",
                     "held_reason", "result_note", "gpu_id", "gpu_name", "start_time", "end_time", "submitted_at")


def _secret_key():
    config.ensure_dirs()
    path = os.path.join(config.DATA_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def create_app(db_path=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=_secret_key(), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      MAX_CONTENT_LENGTH=64 * 1024)
    app.jinja_env.globals.update(csrf_token=auth.csrf_token, workloads=config.WORKLOADS)

    @app.template_filter("dt")
    def fmt_dt(v):
        return time.strftime("%d %b %H:%M:%S", time.localtime(v)) if v else "-"

    @app.before_request
    def _open():
        g.repo = Repository(db_path)
        auth.check_csrf()

    @app.teardown_request
    def _close(exc):
        repo = g.pop("repo", None)
        if repo is not None:
            repo.close()

    @app.after_request
    def _headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.context_processor
    def _ctx():
        return {"user": auth.current_user()}

    def fail(msg, status, **extra):
        if auth.wants_json():
            return jsonify(error=msg, **extra), status
        flash(msg, "error")
        return None

    # ------------------------------------------------------------------ login
    @app.route("/")
    def index():
        return redirect(url_for("new_job") if auth.current_user() else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html")
        data = request.get_json(silent=True) or request.form
        ip = request.remote_addr or "?"
        if auth.too_many_failures(ip):
            return fail("too many failed attempts; wait a minute", 429) or (render_template("login.html"), 429)
        u = g.repo.verify_login(str(data.get("username", "")), str(data.get("password", "")))
        if not u:
            auth.note_failure(ip)
            return fail("wrong username or password", 401) or (render_template("login.html"), 401)
        auth.clear_failures(ip)
        session.clear()
        session["user_id"] = u["user_id"]
        if auth.wants_json():
            return jsonify(ok=True, username=u["username"], role=u["role"])
        nxt = request.args.get("next", "")
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("new_job"))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ jobs
    def job_view(job):
        d = {k: job.get(k) for k in PUBLIC_JOB_FIELDS}
        d["queue_position"] = g.repo.queue_position(job["job_id"]) if job["state"] in ("queued", "held") else None
        d["username"] = job.get("username")
        d["remaining_s"] = max(0, int(job["end_time"] - time.time())) if job["state"] == "running" and job.get("end_time") else None
        return d

    @app.route("/jobs/new")
    @auth.login_required
    def new_job():
        gpus = g.repo.list_active_gpus()
        return render_template("submit.html", errors={}, form={"priority": "3", "duration_min": "1", "memory_gb": "1"},
                               max_gpu=max([x["memory_gb"] for x in gpus], default=0))

    @app.route("/jobs", methods=["GET", "POST"])
    @auth.login_required
    def jobs():
        me = auth.current_user()
        if request.method == "POST":                                         # FR1, FR3
            data = request.get_json(silent=True) if request.is_json else request.form
            if data is None:
                return jsonify(error="invalid JSON"), 400
            clean, errors = validate_job(data)
            if errors:
                if auth.wants_json():
                    field = next(iter(errors))
                    return jsonify(field=field, error=errors[field], errors=errors), 400
                gpus = g.repo.list_active_gpus()
                return render_template("submit.html", errors=errors, form=data,
                                       max_gpu=max([x["memory_gb"] for x in gpus], default=0)), 400
            job_id = g.repo.submit_job(me["user_id"], **clean)
            if auth.wants_json():
                return jsonify(job_id=job_id, state="queued"), 201
            return redirect(url_for("job_detail", job_id=job_id))
        rows = g.repo.list_jobs(None if me["role"] == "admin" else me["user_id"])
        if auth.wants_json():
            return jsonify([job_view(j) for j in rows])
        return render_template("jobs.html", jobs=[job_view(j) for j in rows])

    @app.route("/jobs/<int:job_id>")
    @auth.login_required
    def job_detail(job_id):
        me = auth.current_user()
        job = g.repo.get_job(job_id)
        if job is None or (job["user_id"] != me["user_id"] and me["role"] != "admin"):   # FR18: own jobs only
            if auth.wants_json():
                return jsonify(error="job not found"), 404
            abort(404)
        v = job_view(job)
        return jsonify(v) if auth.wants_json() else render_template("job.html", job=v)

    # ------------------------------------------------------------------ pool (FR17)
    @app.route("/pool")
    @auth.login_required
    def pool():
        me = auth.current_user()
        rows = []
        for r in g.repo.pool_view():
            busy = r["current_job_id"] is not None
            mine = busy and r["run_user_id"] == me["user_id"]
            rows.append({
                "gpu_id": r["gpu_id"], "name": r["name"], "gpu_type": r["gpu_type"], "memory_gb": r["memory_gb"],
                "status": r["status"], "simulated": bool(r["is_simulated"]), "busy": busy,
                "job_id": r["run_job_id"] if (mine or me["role"] == "admin") else None,
                "who": r["run_username"] if me["role"] == "admin" else ("you" if mine else ("another user" if busy else None)),
                "free_in_s": max(0, int(r["run_end_time"] - time.time())) if busy and r["run_end_time"] else None,
                "mem_used_mb": r["mem_used_mb"], "util_pct": r["util_pct"]})
        queue = len(g.repo.list_open_jobs())
        if auth.wants_json():
            return jsonify(gpus=rows, queue_length=queue)
        return render_template("pool.html", gpus=rows, queue=queue)

    # ------------------------------------------------------------------ admin (FR19, FR20)
    @app.route("/admin")
    @auth.admin_required
    def admin():
        held = [job_view(j) for j in g.repo.list_held_jobs()]
        if auth.wants_json():
            return jsonify(gpus=g.repo.list_all_gpus(), held=held)
        return render_template("admin.html", gpus=g.repo.list_all_gpus(), held=held,
                               allow_sim=config.ALLOW_SIMULATED_GPUS)

    @app.route("/admin/held")
    @auth.admin_required
    def admin_held():
        return jsonify([job_view(j) for j in g.repo.list_held_jobs()])

    def admin_result(msg, status=200, ok=True):
        if auth.wants_json():
            return jsonify(ok=ok, message=msg) if ok else (jsonify(error=msg), status)
        flash(msg, "ok" if ok else "error")
        return redirect(url_for("admin"))

    @app.route("/admin/gpus", methods=["POST"])
    @auth.admin_required
    def admin_add_gpu():
        if not config.ALLOW_SIMULATED_GPUS:
            return admin_result("simulated GPU records are disabled until the guide approves them; "
                                "real GPUs are discovered automatically", 403, ok=False)
        data = request.get_json(silent=True) if request.is_json else request.form
        clean, errors = validate_gpu(data or {})
        if errors:
            return admin_result("; ".join(f"{k}: {v}" for k, v in errors.items()), 400, ok=False)
        gid = g.repo.add_gpu(clean["name"], clean["gpu_type"], clean["memory_gb"], is_simulated=1)
        return admin_result(f"GPU {gid} added (simulated)")

    @app.route("/admin/gpus/<int:gpu_id>/remove", methods=["POST"])
    @auth.admin_required
    def admin_remove_gpu(gpu_id):
        try:
            g.repo.remove_gpu(gpu_id)
        except GpuBusy:
            return admin_result("cannot remove: a job is running on this GPU (wait until it finishes)", 409, ok=False)
        except NotFound:
            return admin_result("GPU not found", 404, ok=False)
        return admin_result(f"GPU {gpu_id} removed from the pool")

    @app.route("/admin/gpus/<int:gpu_id>/restore", methods=["POST"])
    @auth.admin_required
    def admin_restore_gpu(gpu_id):
        try:
            g.repo.restore_gpu(gpu_id)
        except NotFound:
            return admin_result("GPU not found", 404, ok=False)
        return admin_result(f"GPU {gpu_id} is back in the pool")

    @app.route("/admin/gpus/<int:gpu_id>/rename", methods=["POST"])
    @auth.admin_required
    def admin_rename_gpu(gpu_id):
        data = request.get_json(silent=True) if request.is_json else request.form
        name = str((data or {}).get("name", "")).strip()
        if not 1 <= len(name) <= 60:
            return admin_result("name must be 1 to 60 characters", 400, ok=False)
        try:
            g.repo.rename_gpu(gpu_id, name)
        except NotFound:
            return admin_result("GPU not found", 404, ok=False)
        return admin_result("GPU renamed")

    @app.errorhandler(403)
    def _403(e):
        return (jsonify(error="not permitted"), 403) if auth.wants_json() else (render_template("error.html", code=403, msg="You are not allowed to do that."), 403)

    @app.errorhandler(404)
    def _404(e):
        return (jsonify(error="not found"), 404) if auth.wants_json() else (render_template("error.html", code=404, msg="Page or job not found."), 404)

    @app.errorhandler(400)
    def _400(e):
        return (jsonify(error=getattr(e, "description", "bad request")), 400) if auth.wants_json() else (render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400)

    return app


def main():
    app = create_app()
    print(f"[E4] web server on http://{config.HOST}:{config.PORT}", flush=True)
    app.run(host=config.HOST, port=config.PORT, threaded=True, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
