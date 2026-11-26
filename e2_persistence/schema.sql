-- E2 schema. Times are stored as epoch seconds (REAL).
CREATE TABLE IF NOT EXISTS app_user (
    user_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('submitter','admin'))
);

CREATE TABLE IF NOT EXISTS gpu (
    gpu_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT NOT NULL,
    gpu_type       TEXT NOT NULL,
    memory_gb      REAL NOT NULL CHECK (memory_gb > 0),
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','removed')),
    is_simulated   INTEGER NOT NULL DEFAULT 0,
    device_index   INTEGER,                 -- nvidia-smi index for real GPUs
    current_job_id INTEGER REFERENCES job(job_id),   -- NULL = free (HC2)
    free_since     REAL NOT NULL,           -- when the GPU last became free (used by SP2)
    mem_used_mb    REAL,                    -- live stats written by E3
    util_pct       REAL,
    stats_at       REAL
);

CREATE TABLE IF NOT EXISTS job (
    job_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES app_user(user_id),
    template     TEXT NOT NULL CHECK (template IN ('matmul','train')),
    memory_gb    REAL NOT NULL CHECK (memory_gb > 0),
    duration_min REAL NOT NULL CHECK (duration_min > 0),
    priority     INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
    deadline     TEXT,                      -- metadata only (FR2), never used for scheduling
    submitted_at REAL NOT NULL,
    state        TEXT NOT NULL CHECK (state IN ('queued','held','running','completed')),
    held_reason  TEXT,
    result_note  TEXT
);

CREATE TABLE IF NOT EXISTS assignment (
    assignment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL UNIQUE REFERENCES job(job_id),
    gpu_id        INTEGER NOT NULL REFERENCES gpu(gpu_id),
    start_time    REAL NOT NULL,
    end_time      REAL NOT NULL,
    deciding_rule TEXT NOT NULL,
    committed_at  REAL NOT NULL,
    launched      INTEGER NOT NULL DEFAULT 0,
    pid           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_job_state ON job(state);
CREATE INDEX IF NOT EXISTS idx_job_user ON job(user_id);
