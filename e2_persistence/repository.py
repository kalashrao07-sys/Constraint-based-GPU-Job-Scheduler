"""E2 Persistence & Transaction Engine: the ONLY code that talks SQL.

Other engines call these methods and get plain dicts back.
The key guarantee is commit_assignment(): the assignment row, the job state change and the
GPU occupancy update are written in ONE transaction (all or nothing, NFR1), and HC1 / HC2 / HC3
are re-checked inside it, so no caller can ever store an invalid assignment.
"""
import os
import sqlite3
import time
from contextlib import contextmanager

from werkzeug.security import check_password_hash, generate_password_hash

from . import db


class NotFound(Exception):
    pass


class BadState(Exception):
    pass


class GpuBusy(Exception):
    """Raised when a GPU with a running job is removed (UC6)."""


class AssignmentRejected(Exception):
    """Raised when a commit would break HC1, HC2 or the state rules; nothing is written."""


def _d(row):
    return dict(row) if row is not None else None


class Repository:
    def __init__(self, path=None):
        self._path = path
        self._conn = None

    # ------------------------------------------------------------------ plumbing
    @property
    def conn(self):
        if self._conn is None:
            self._conn = db.connect(self._path)
        return self._conn

    def init(self):
        db.init_schema(self.conn)

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ------------------------------------------------------------------ users
    def create_user(self, username, password, role="submitter"):
        h = generate_password_hash(password, method="pbkdf2:sha256")
        with self._tx() as c:
            cur = c.execute("INSERT INTO app_user(username,password_hash,role) VALUES(?,?,?)",
                            (username, h, role))
            return cur.lastrowid

    def verify_login(self, username, password):
        row = self.conn.execute("SELECT * FROM app_user WHERE username=?", (username,)).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            return {"user_id": row["user_id"], "username": row["username"], "role": row["role"]}
        return None

    def get_user(self, user_id):
        row = self.conn.execute("SELECT user_id,username,role FROM app_user WHERE user_id=?",
                                (user_id,)).fetchone()
        return _d(row)

    # ------------------------------------------------------------------ jobs
    def submit_job(self, user_id, template, memory_gb, duration_min, priority, deadline=None, now=None):
        """Store a validated job as 'queued' (hand-off from E4 to E1)."""
        now = time.time() if now is None else now
        try:
            with self._tx() as c:
                cur = c.execute(
                    "INSERT INTO job(user_id,template,memory_gb,duration_min,priority,deadline,submitted_at,state)"
                    " VALUES(?,?,?,?,?,?,?,'queued')",
                    (user_id, template, memory_gb, duration_min, priority, deadline, now))
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError(f"job rejected by database constraints: {e}")

    _JOB_SELECT = (
        "SELECT j.*, u.username, a.assignment_id, a.gpu_id, a.start_time, a.end_time, a.deciding_rule, "
        "a.launched, a.pid, g.name AS gpu_name FROM job j JOIN app_user u ON u.user_id=j.user_id "
        "LEFT JOIN assignment a ON a.job_id=j.job_id LEFT JOIN gpu g ON g.gpu_id=a.gpu_id ")

    def get_job(self, job_id):
        return _d(self.conn.execute(self._JOB_SELECT + "WHERE j.job_id=?", (job_id,)).fetchone())

    def list_jobs(self, user_id=None, limit=200):
        sql = self._JOB_SELECT + ("WHERE j.user_id=? " if user_id is not None else "")
        sql += "ORDER BY j.job_id DESC LIMIT ?"
        args = (user_id, limit) if user_id is not None else (limit,)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def list_open_jobs(self):
        """Queued + held jobs for E1. Each carries pref_gpu_type: the GPU type this user's
        most recent job ran on (used by SP3, GPU-type continuity)."""
        sql = ("SELECT j.*, (SELECT g.gpu_type FROM assignment a JOIN job j2 ON j2.job_id=a.job_id "
               "JOIN gpu g ON g.gpu_id=a.gpu_id WHERE j2.user_id=j.user_id "
               "ORDER BY a.committed_at DESC LIMIT 1) AS pref_gpu_type "
               "FROM job j WHERE j.state IN ('queued','held')")
        return [dict(r) for r in self.conn.execute(sql)]

    def list_held_jobs(self):
        return [dict(r) for r in self.conn.execute(
            self._JOB_SELECT.replace("a.assignment_id,", "NULL AS assignment_id,") +
            "WHERE j.state='held' ORDER BY j.priority DESC, j.submitted_at")]

    def queue_position(self, job_id):
        """1-based place among open jobs in scheduling order (priority desc, submission asc)."""
        rows = self.conn.execute(
            "SELECT job_id FROM job WHERE state IN ('queued','held') ORDER BY priority DESC, submitted_at, job_id"
        ).fetchall()
        for i, r in enumerate(rows, 1):
            if r["job_id"] == job_id:
                return i
        return None

    def set_held(self, job_id, reason):
        """queued/held -> held with a specific reason (FR11 / FR12). Returns True if changed."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE job SET state='held', held_reason=? WHERE job_id=? AND state IN ('queued','held') "
                "AND (state!='held' OR held_reason IS NOT ?)", (reason, job_id, reason))
            return cur.rowcount > 0

    # ------------------------------------------------------------------ GPUs
    def add_gpu(self, name, gpu_type, memory_gb, is_simulated=0, device_index=None, now=None):
        now = time.time() if now is None else now
        try:
            with self._tx() as c:
                cur = c.execute(
                    "INSERT INTO gpu(name,gpu_type,memory_gb,is_simulated,device_index,free_since) VALUES(?,?,?,?,?,?)",
                    (name, gpu_type, memory_gb, int(is_simulated), device_index, now))
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError(f"GPU rejected by database constraints: {e}")

    def upsert_real_gpu(self, device_index, name, gpu_type, memory_gb, now=None):
        """Register a GPU found by nvidia-smi. Existing rows keep their status (admin choice)."""
        now = time.time() if now is None else now
        with self._tx() as c:
            row = c.execute("SELECT gpu_id FROM gpu WHERE device_index=? AND is_simulated=0",
                            (device_index,)).fetchone()
            if row:
                c.execute("UPDATE gpu SET name=?, gpu_type=?, memory_gb=? WHERE gpu_id=?",
                          (name, gpu_type, memory_gb, row["gpu_id"]))
                return row["gpu_id"]
            cur = c.execute(
                "INSERT INTO gpu(name,gpu_type,memory_gb,is_simulated,device_index,free_since) VALUES(?,?,?,0,?,?)",
                (name, gpu_type, memory_gb, device_index, now))
            return cur.lastrowid

    def list_active_gpus(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM gpu WHERE status='active' ORDER BY gpu_id")]

    def list_all_gpus(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM gpu ORDER BY gpu_id")]

    def pool_view(self):
        """Every GPU with the job currently on it (if any), for the pool page."""
        sql = ("SELECT g.*, j.job_id AS run_job_id, j.template AS run_template, j.user_id AS run_user_id, "
               "u.username AS run_username, a.end_time AS run_end_time FROM gpu g "
               "LEFT JOIN job j ON j.job_id=g.current_job_id LEFT JOIN app_user u ON u.user_id=j.user_id "
               "LEFT JOIN assignment a ON a.job_id=j.job_id ORDER BY g.gpu_id")
        return [dict(r) for r in self.conn.execute(sql)]

    def remove_gpu(self, gpu_id):
        """Admin removes a GPU from the pool; blocked while a job runs on it (UC6)."""
        with self._tx() as c:
            row = c.execute("SELECT * FROM gpu WHERE gpu_id=?", (gpu_id,)).fetchone()
            if row is None:
                raise NotFound(f"GPU {gpu_id} not found")
            if row["current_job_id"] is not None:
                raise GpuBusy(f"GPU {gpu_id} has a running job")
            c.execute("UPDATE gpu SET status='removed' WHERE gpu_id=?", (gpu_id,))

    def restore_gpu(self, gpu_id, now=None):
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute("UPDATE gpu SET status='active', free_since=? WHERE gpu_id=?", (now, gpu_id))
            if cur.rowcount == 0:
                raise NotFound(f"GPU {gpu_id} not found")

    def rename_gpu(self, gpu_id, name):
        with self._tx() as c:
            cur = c.execute("UPDATE gpu SET name=? WHERE gpu_id=?", (name, gpu_id))
            if cur.rowcount == 0:
                raise NotFound(f"GPU {gpu_id} not found")

    def update_gpu_stats(self, gpu_id, mem_used_mb, util_pct, now=None):
        now = time.time() if now is None else now
        with self._tx() as c:
            c.execute("UPDATE gpu SET mem_used_mb=?, util_pct=?, stats_at=? WHERE gpu_id=?",
                      (mem_used_mb, util_pct, now, gpu_id))

    # ------------------------------------------------------------------ the atomic commit (NFR1, HC3)
    def commit_assignment(self, job_id, gpu_id, start_time, end_time, rule, now=None, _fail_before_commit=False):
        """ONE transaction: INSERT assignment + job -> running + GPU occupied.
        HC1 and HC2 are re-checked inside, so an invalid assignment can never be stored."""
        now = time.time() if now is None else now
        with self._tx() as c:
            job = c.execute("SELECT * FROM job WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise NotFound(f"job {job_id} not found")
            if job["state"] not in ("queued", "held"):
                raise BadState(f"job {job_id} is {job['state']}, cannot be assigned")
            gpu = c.execute("SELECT * FROM gpu WHERE gpu_id=?", (gpu_id,)).fetchone()
            if gpu is None or gpu["status"] != "active":
                raise AssignmentRejected("GPU is not an active pool member")
            if gpu["memory_gb"] < job["memory_gb"]:
                raise AssignmentRejected("HC1 violated: GPU memory below the job requirement")
            cur = c.execute("UPDATE gpu SET current_job_id=? WHERE gpu_id=? AND current_job_id IS NULL",
                            (job_id, gpu_id))
            if cur.rowcount != 1:
                raise AssignmentRejected("HC2 violated: GPU already has a running job")
            c.execute("INSERT INTO assignment(job_id,gpu_id,start_time,end_time,deciding_rule,committed_at) "
                      "VALUES(?,?,?,?,?,?)", (job_id, gpu_id, start_time, end_time, rule, now))
            c.execute("UPDATE job SET state='running', held_reason=NULL WHERE job_id=?", (job_id,))
            # ---- test hooks: used only by the NFR1 / NFR4 tests to simulate a crash before COMMIT
            if _fail_before_commit:
                raise RuntimeError("injected failure before COMMIT")
            if os.environ.get("GPUSCHED_FAULT") == "sleep_in_commit":
                mark = os.environ.get("GPUSCHED_FAULT_MARK")
                if mark:                        # tells the test "I am inside the transaction now"
                    with open(mark, "w") as f:
                        f.write(str(os.getpid()))
                time.sleep(float(os.environ.get("GPUSCHED_FAULT_SLEEP", "3")))
        return True

    # ------------------------------------------------------------------ run lifecycle (E3)
    def unlaunched_assignments(self):
        sql = ("SELECT a.*, j.template, j.memory_gb, j.duration_min, g.device_index, g.is_simulated "
               "FROM assignment a JOIN job j ON j.job_id=a.job_id JOIN gpu g ON g.gpu_id=a.gpu_id "
               "WHERE j.state='running' AND a.launched=0 ORDER BY a.assignment_id")
        return [dict(r) for r in self.conn.execute(sql)]

    def running_assignments(self):
        sql = ("SELECT a.*, j.template, j.memory_gb, j.duration_min, g.device_index "
               "FROM assignment a JOIN job j ON j.job_id=a.job_id JOIN gpu g ON g.gpu_id=a.gpu_id "
               "WHERE j.state='running' ORDER BY a.assignment_id")
        return [dict(r) for r in self.conn.execute(sql)]

    def mark_launched(self, assignment_id, pid):
        with self._tx() as c:
            c.execute("UPDATE assignment SET launched=1, pid=? WHERE assignment_id=?", (pid, assignment_id))

    def complete_job(self, job_id, result_note, now=None):
        """running -> completed; releases the GPU (FR16). Returns False if not running."""
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute("UPDATE job SET state='completed', result_note=? WHERE job_id=? AND state='running'",
                            (result_note, job_id))
            if cur.rowcount == 0:
                return False
            c.execute("UPDATE gpu SET current_job_id=NULL, free_since=? WHERE current_job_id=?", (now, job_id))
            return True

    def requeue_job(self, job_id, note, now=None):
        """Recovery: a running job whose process is gone goes back to queued and frees its GPU."""
        now = time.time() if now is None else now
        with self._tx() as c:
            cur = c.execute("UPDATE job SET state='queued', held_reason=NULL, result_note=? "
                            "WHERE job_id=? AND state='running'", (note, job_id))
            if cur.rowcount == 0:
                return False
            c.execute("DELETE FROM assignment WHERE job_id=?", (job_id,))
            c.execute("UPDATE gpu SET current_job_id=NULL, free_since=? WHERE current_job_id=?", (now, job_id))
            return True

    # ------------------------------------------------------------------ integrity check (NFR2 / NFR4 / NFR9)
    def check_consistency(self):
        """Return a list of problems. An empty list means the stored state is consistent."""
        q = lambda s: [dict(r) for r in self.conn.execute(s)]
        problems = []
        for r in q("SELECT j.job_id FROM job j LEFT JOIN assignment a ON a.job_id=j.job_id "
                   "WHERE j.state='running' AND a.job_id IS NULL"):
            problems.append(f"job {r['job_id']} is running without an assignment (HC3)")
        for r in q("SELECT a.job_id FROM assignment a JOIN job j ON j.job_id=a.job_id "
                   "WHERE j.state IN ('queued','held')"):
            problems.append(f"job {r['job_id']} has an assignment but is not running/completed")
        for r in q("SELECT j.job_id FROM job j JOIN assignment a ON a.job_id=j.job_id "
                   "JOIN gpu g ON g.gpu_id=a.gpu_id WHERE j.state='running' AND g.current_job_id IS NOT j.job_id"):
            problems.append(f"job {r['job_id']} is running but its GPU does not show it as occupied")
        for r in q("SELECT g.gpu_id, g.current_job_id FROM gpu g LEFT JOIN job j ON j.job_id=g.current_job_id "
                   "WHERE g.current_job_id IS NOT NULL AND (j.job_id IS NULL OR j.state!='running')"):
            problems.append(f"GPU {r['gpu_id']} is marked busy with job {r['current_job_id']}, which is not running")
        for r in q("SELECT a.gpu_id, COUNT(*) n FROM assignment a JOIN job j ON j.job_id=a.job_id "
                   "WHERE j.state='running' GROUP BY a.gpu_id HAVING n>1"):
            problems.append(f"GPU {r['gpu_id']} has {r['n']} running jobs (HC2)")
        for r in q("SELECT j.job_id FROM job j JOIN assignment a ON a.job_id=j.job_id "
                   "JOIN gpu g ON g.gpu_id=a.gpu_id WHERE j.state='running' AND g.memory_gb < j.memory_gb"):
            problems.append(f"job {r['job_id']} runs on a GPU with too little memory (HC1)")
        return problems
