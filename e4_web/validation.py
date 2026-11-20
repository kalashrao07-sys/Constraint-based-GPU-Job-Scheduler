"""E4: input validation. Runs on the server for every request (browser checks can be bypassed)."""
import math
from datetime import datetime

import config


def _num(v):
    if isinstance(v, bool):
        raise ValueError
    x = float(v)
    if not math.isfinite(x):
        raise ValueError
    return x


def validate_job(data):
    """Return (cleaned, errors). errors maps field name -> message; empty means valid (FR1, FR3)."""
    clean, err = {}, {}
    t = (data.get("template") or "").strip() if isinstance(data.get("template"), str) else data.get("template")
    if t not in config.WORKLOADS:
        err["template"] = "choose one of: " + ", ".join(config.WORKLOADS)
    else:
        clean["template"] = t
    try:
        m = _num(data.get("memory_gb"))
        if m <= 0 or m > config.MAX_MEMORY_GB:
            raise ValueError
        clean["memory_gb"] = m
    except (TypeError, ValueError):
        err["memory_gb"] = f"must be a number above 0 and at most {config.MAX_MEMORY_GB}"
    try:
        d = _num(data.get("duration_min"))
        if d <= 0 or d > config.MAX_DURATION_MIN:
            raise ValueError
        clean["duration_min"] = d
    except (TypeError, ValueError):
        err["duration_min"] = f"must be a number above 0 and at most {config.MAX_DURATION_MIN} (minutes)"
    try:
        raw = data.get("priority")
        if isinstance(raw, bool) or (isinstance(raw, float) and raw != int(raw)):
            raise ValueError
        p = int(str(raw).strip()) if not isinstance(raw, int) else raw
        if not 1 <= p <= 5:
            raise ValueError
        clean["priority"] = p
    except (TypeError, ValueError):
        err["priority"] = "must be a whole number between 1 and 5"
    dl = data.get("deadline")
    if dl in (None, ""):
        clean["deadline"] = None                      # optional, metadata only (FR2)
    else:
        try:
            clean["deadline"] = datetime.fromisoformat(str(dl).strip()).isoformat(timespec="minutes")
        except ValueError:
            err["deadline"] = "use a date like 2026-11-05 or 2026-11-05T18:30 (or leave empty)"
    return clean, err


def validate_gpu(data):
    clean, err = {}, {}
    name = str(data.get("name") or "").strip()
    gtype = str(data.get("gpu_type") or "").strip()
    if not 1 <= len(name) <= 60:
        err["name"] = "1 to 60 characters"
    else:
        clean["name"] = name
    if not 1 <= len(gtype) <= 30:
        err["gpu_type"] = "1 to 30 characters"
    else:
        clean["gpu_type"] = gtype
    try:
        m = _num(data.get("memory_gb"))
        if m <= 0 or m > 1024:
            raise ValueError
        clean["memory_gb"] = m
    except (TypeError, ValueError):
        err["memory_gb"] = "must be a number above 0"
    return clean, err
