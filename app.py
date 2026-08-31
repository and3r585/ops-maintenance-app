#!/usr/bin/env python3
"""
Role-based Site Portal app.
Single-file server: Python standard library only (no pip installs, no network).

  python3 app.py            # serves http://localhost:8000
  python3 app.py --port 9000
  python3 app.py --reset    # wipe data + reseed

Roles:
  admin / admin123          -> ADMIN       (planning + assets)
  <technicians>             -> TECHNICIAN  (24 from the Manplan 2025 tab; all use tech123)
                               usernames are first-initial + surname, e.g. sclydesdale, scant
"""

import argparse
import csv
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import seed_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('ADMIN','TECHNICIAN')),
    display_name  TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tag          TEXT UNIQUE NOT NULL,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL,
    location     TEXT NOT NULL,
    criticality  TEXT NOT NULL DEFAULT 'MEDIUM',
    manufacturer TEXT,
    model        TEXT,
    family       TEXT,
    serial       TEXT,
    install_date TEXT,
    toc          TEXT,               -- Take-over certificate date
    warranty_expiry TEXT,
    defect       TEXT,               -- KGH 2025 col G: defects/issues affecting work/operation
    -- condition-monitoring snapshot, from the KGH SMP Action Tracker tab
    smp_data_date     TEXT,
    smp_gearbox       TEXT,
    smp_generator     TEXT,
    smp_main_bearing  TEXT,
    smp_observations  TEXT,
    created_at   TEXT NOT NULL
);

-- Technician day roster: only days a technician is NOT available.
-- code in ('HOL in WD','MED','SICK','ABS','TRG','PAT','JURY'); from the Manplan 2025 tab.
CREATE TABLE IF NOT EXISTS roster (
    user_id INTEGER NOT NULL REFERENCES users(id),
    on_date TEXT NOT NULL,
    code    TEXT NOT NULL,
    PRIMARY KEY (user_id, on_date)
);

-- Work-order history per asset (from the Job Request "SCOTT & STUART 2026" tab).
CREATE TABLE IF NOT EXISTS asset_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id      INTEGER NOT NULL REFERENCES assets(id),
    occurred_on   TEXT,
    description   TEXT NOT NULL,
    work_type     TEXT,
    service_order TEXT,
    technicians   TEXT
);

-- Per-asset dated records: service completions, retrofits, (later) component info.
CREATE TABLE IF NOT EXISTS asset_records (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    INTEGER NOT NULL REFERENCES assets(id),
    category    TEXT NOT NULL,          -- 'service' | 'retrofit' | 'component'
    name        TEXT NOT NULL,
    occurred_on TEXT,                   -- ISO date, or NULL if not recorded
    detail      TEXT,
    status      TEXT,                   -- retrofits: 'complete' | 'in_progress' | 'outstanding'
    sort        INTEGER NOT NULL DEFAULT 0
);

-- Daily team plan: 10 team rows per date, each an (optional) job + its technicians.
CREATE TABLE IF NOT EXISTS plan_team (
    plan_date TEXT NOT NULL,
    team_no   INTEGER NOT NULL,
    job_id    INTEGER REFERENCES jobs(id),
    PRIMARY KEY (plan_date, team_no)
);
CREATE TABLE IF NOT EXISTS plan_member (
    plan_date TEXT NOT NULL,
    team_no   INTEGER NOT NULL,
    user_id   INTEGER NOT NULL REFERENCES users(id),
    PRIMARY KEY (plan_date, team_no, user_id)
);

CREATE TABLE IF NOT EXISTS pending_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id),
    author_id  INTEGER NOT NULL REFERENCES users(id),
    note       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'SUBMITTED',
    wo_code    TEXT,                  -- SAP/SGRE notification number (imported from Pendings)
    priority   INTEGER,               -- SGRE priority (1 highest .. 6 lowest)
    system     TEXT,                  -- affected turbine system
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_photos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_entry_id  INTEGER NOT NULL REFERENCES pending_entries(id),
    filename          TEXT NOT NULL,
    caption           TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    description    TEXT,
    asset_id       INTEGER REFERENCES assets(id),
    priority       TEXT NOT NULL DEFAULT 'MEDIUM',
    status         TEXT NOT NULL DEFAULT 'OUTSTANDING',
    estimated_minutes INTEGER,
    due_date       TEXT,
    assignee_id    INTEGER REFERENCES users(id),
    scheduled_date TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_activity (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER NOT NULL REFERENCES jobs(id),
    user_id    INTEGER REFERENCES users(id),
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Module registry: drives navigation so new function areas can be bolted on later.
CREATE TABLE IF NOT EXISTS modules (
    key      TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    min_role TEXT NOT NULL DEFAULT 'TECHNICIAN',
    sort     INTEGER NOT NULL DEFAULT 0
);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def add_months(iso_date, months):
    """Add whole months to an ISO date string, clamping the day."""
    if not iso_date:
        return None
    import calendar
    y, m, d = (int(x) for x in iso_date[:10].split("-"))
    total = (m - 1) + months
    y += total // 12
    m = total % 12 + 1
    d = min(d, calendar.monthrange(y, m)[1])
    return "%04d-%02d-%02d" % (y, m, d)


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 200_000)
    return dk.hex(), salt


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def seed():
    conn = get_db()
    conn.executescript(SCHEMA)

    data = seed_data.load()

    # --- users: admin + the technicians from Manplan 2025.csv (col E, rows 14-37) ---
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        rows = [("admin", "admin123", "ADMIN", "Alex Reid")]
        for t in data["technicians"]:
            rows.append((t["username"], "tech123", "TECHNICIAN", t["name"]))
        for username, pw, role, name in rows:
            ph, salt = hash_password(pw)
            conn.execute(
                "INSERT INTO users (username,password_hash,salt,role,display_name,active,created_at)"
                " VALUES (?,?,?,?,?,1,?)",
                (username, ph, salt, role, name, now()),
            )

    # --- assets: one per turbine in KGH 2025.csv (+ its defect note, col G) ---
    if conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"] == 0:
        for tid in data["turbines"]:
            conn.execute(
                "INSERT INTO assets (tag,name,type,location,defect,created_at) VALUES (?,?,?,?,?,?)",
                (tid, tid, "Wind Turbine", "Array " + tid[0],
                 data["defects"].get(tid), now()),
            )

    # --- equipment: Equipment info.csv ---
    if conn.execute(
        "SELECT COUNT(*) c FROM assets WHERE manufacturer IS NOT NULL"
    ).fetchone()["c"] == 0:
        for tag, e in data["equipment"].items():
            conn.execute(
                "UPDATE assets SET manufacturer=?, model=?, family=?, serial=?, "
                "install_date=?, toc=?, warranty_expiry=? WHERE tag=?",
                (e["manufacturer"], e["model"], e["family"], e["serial"],
                 e["install_date"], e["toc"], e["warranty_expiry"], tag),
            )

    # --- condition monitoring: KGH SMP Action Tracker.csv ---
    if conn.execute("SELECT COUNT(*) c FROM assets WHERE smp_gearbox IS NOT NULL").fetchone()["c"] == 0:
        for tag, s in data["smp"].items():
            conn.execute(
                "UPDATE assets SET smp_data_date=?, smp_gearbox=?, smp_generator=?, "
                "smp_main_bearing=?, smp_observations=? WHERE tag=?",
                (s["data_date"], s["gearbox"], s["generator"],
                 s["main_bearing"], s["observations"], tag),
            )

    # --- service / HV / stat / retrofit / component records ---
    if conn.execute("SELECT COUNT(*) c FROM asset_records").fetchone()["c"] == 0:
        aid = {r["tag"]: r["id"] for r in conn.execute("SELECT id,tag FROM assets")}

        def add_records(by_tag, category, with_status=False):
            for tag, recs in by_tag.items():
                if tag not in aid:
                    continue
                for rec in recs:
                    if with_status:
                        conn.execute(
                            "INSERT INTO asset_records (asset_id,category,name,occurred_on,detail,status,sort) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (aid[tag], category, rec["name"], rec.get("date"),
                             rec.get("detail"), rec.get("status"), rec.get("sort", 0)),
                        )
                    else:
                        conn.execute(
                            "INSERT INTO asset_records (asset_id,category,name,occurred_on,detail,sort) "
                            "VALUES (?,?,?,?,?,?)",
                            (aid[tag], category, rec["name"], rec.get("date"),
                             rec.get("detail"), rec.get("sort", 0)),
                        )

        add_records(data["services"], "service")
        add_records(data["hv"], "hv")
        add_records(data["stat"], "stat")
        add_records(data["retrofits"], "retrofit", with_status=True)
        add_records(data["components"], "component")

    # --- technician roster (unavailable days): Manplan 2025.csv ---
    if conn.execute("SELECT COUNT(*) c FROM roster").fetchone()["c"] == 0:
        uid = {r["username"]: r["id"] for r in conn.execute("SELECT id,username FROM users")}
        for on_date, people in data["roster"].items():
            for username, code in people.items():
                if username in uid:
                    conn.execute(
                        "INSERT OR IGNORE INTO roster (user_id,on_date,code) VALUES (?,?,?)",
                        (uid[username], on_date, code),
                    )

    # --- work-order history: Job Request.csv ---
    if conn.execute("SELECT COUNT(*) c FROM asset_history").fetchone()["c"] == 0:
        aid = {r["tag"]: r["id"] for r in conn.execute("SELECT id,tag FROM assets")}
        for tag, entries in data["history"].items():
            if tag not in aid:
                continue
            for e in entries:
                conn.execute(
                    "INSERT INTO asset_history "
                    "(asset_id,occurred_on,description,work_type,service_order,technicians) "
                    "VALUES (?,?,?,?,?,?)",
                    (aid[tag], e.get("date"), e.get("description") or "Work order",
                     e.get("work_type"), e.get("service_order"), e.get("technicians")),
                )

    if conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"] == 0:
        # (title, turbine, priority, status, est_min, due_date, assignee_username, scheduled_date)
        jobs = [
            ("72 month major service", "A01", "HIGH", "OUTSTANDING", 480, "2026-09-10", None, None),
            ("Pitch system fault - investigate", "C19", "URGENT", "OUTSTANDING", 240, "2026-09-02", None, None),
            ("Blade leading-edge inspection", "A05", "MEDIUM", "OUTSTANDING", 180, "2026-09-12", None, None),
            ("Gearbox oil sample and filter change", "B12", "MEDIUM", "OUTSTANDING", 120, "2026-09-08", None, None),
            ("Yaw brake pad replacement", "D27", "HIGH", "OUTSTANDING", 300, "2026-09-05", None, None),
            ("Annual statutory inspection", "E37", "HIGH", "OUTSTANDING", 90, "2026-09-04", None, None),
            ("Converter cooling fan replacement", "F47", "MEDIUM", "OUTSTANDING", 150, "2026-09-15", None, None),
            ("HV maintenance - transformer", "G57", "HIGH", "OUTSTANDING", 360, "2026-09-18", None, None),
            ("6 month lift inspection", "H67", "LOW", "OUTSTANDING", 60, "2026-09-03", None, None),
            ("Nacelle anemometer swap", "J87", "LOW", "OUTSTANDING", 45, "2026-09-06", None, None),
            ("84 month major service", "I77", "HIGH", "OUTSTANDING", 480, "2026-09-09", None, None),
            ("Down-tower bolt torque check", "B15", "LOW", "OUTSTANDING", 120, "2026-09-04", None, None),
        ]
        aid = {r["tag"]: r["id"] for r in conn.execute("SELECT id,tag FROM assets")}
        uid = {r["username"]: r["id"] for r in conn.execute("SELECT id,username FROM users")}
        for t in jobs:
            conn.execute(
                "INSERT INTO jobs (title,description,asset_id,priority,status,estimated_minutes,"
                "due_date,assignee_id,scheduled_date,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (t[0], "", aid.get(t[1]), t[2], t[3], t[4], t[5],
                 uid.get(t[6]) if t[6] else None, t[7], now(), now()),
            )

    # --- pending entries: imported from Pendings.csv (open SGRE/SAP notifications) ---
    if conn.execute("SELECT COUNT(*) c FROM pending_entries").fetchone()["c"] == 0:
        aid = {r["tag"]: r["id"] for r in conn.execute("SELECT id,tag FROM assets")}
        admin_id = conn.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()["id"]
        for p in data["pendings"]:
            if p["tag"] not in aid:
                continue
            conn.execute(
                "INSERT INTO pending_entries "
                "(asset_id,author_id,note,status,wo_code,priority,system,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (aid[p["tag"]], admin_id, p["note"], p["status"],
                 p["wo_code"], p["priority"], p["system"], p["created_at"] or now()),
            )

    if conn.execute("SELECT COUNT(*) c FROM modules").fetchone()["c"] == 0:
        for key, name, min_role, sort in [
            ("assets", "Asset Information", "TECHNICIAN", 10),
            ("planning", "Planning", "ADMIN", 20),
            ("dashboard", "Site Dashboard", "ADMIN", 5),
        ]:
            conn.execute(
                "INSERT INTO modules (key,name,enabled,min_role,sort) VALUES (?,?,1,?,?)",
                (key, name, min_role, sort),
            )

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Multipart form parsing (for photo uploads)
# ---------------------------------------------------------------------------

def parse_multipart(body, boundary):
    """Return (fields dict, files list of {name, filename, content})."""
    fields, files = {}, []
    sep = b"--" + boundary.encode()
    for part in body.split(sep):
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        headers = {}
        for line in raw_headers.decode("utf-8", "replace").split("\r\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name_m = re.search(r'name="([^"]*)"', disp)
        file_m = re.search(r'filename="([^"]*)"', disp)
        if not name_m:
            continue
        name = name_m.group(1)
        if file_m and file_m.group(1):
            files.append({"name": name, "filename": file_m.group(1), "content": content})
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


class RawResponse:
    """Return from a route to send a non-JSON body (e.g. a CSV download)."""
    def __init__(self, body, content_type, filename=None):
        self.body = body if isinstance(body, bytes) else body.encode("utf-8")
        self.content_type = content_type
        self.filename = filename


class Handler(BaseHTTPRequestHandler):
    server_version = "OpsApp/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers --------------------------------------------------------------

    def _send_json(self, obj, status=200):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_raw(self, r, status=200):
        self.send_response(status)
        self.send_header("Content-Type", r.content_type)
        self.send_header("Content-Length", str(len(r.body)))
        if r.filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % r.filename)
        self.end_headers()
        self.wfile.write(r.body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def _json_body(self):
        raw = self._body()
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError(400, "Invalid JSON")

    def _current_user(self, conn):
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        token = auth[7:]
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        return row

    def _require(self, conn, role=None):
        user = self._current_user(conn)
        if not user:
            raise ApiError(401, "Not authenticated")
        if role and user["role"] != role:
            raise ApiError(403, "Forbidden")
        return user

    # -- dispatch ----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PATCH(self):
        self._dispatch("PATCH")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path.startswith("/api/"):
                conn = get_db()
                try:
                    result = self._route_api(method, path, parse_qs(parsed.query), conn)
                    conn.commit()
                finally:
                    conn.close()
                if result is None:
                    raise ApiError(404, "Not found")
                status, obj = result
                if isinstance(obj, RawResponse):
                    self._send_raw(obj, status)
                else:
                    self._send_json(obj, status)
            elif path.startswith("/uploads/"):
                self._serve_upload(path[len("/uploads/"):])
            else:
                self._serve_static(path)
        except ApiError as e:
            self._send_json({"error": e.message}, e.status)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa
            sys.stderr.write("  ! server error: %r\n" % e)
            self._send_json({"error": "Internal server error"}, 500)

    # -- API routes -------------------------------------------------------

    def _route_api(self, method, path, query, conn):
        parts = [p for p in path.split("/") if p][1:]  # drop 'api'

        # --- auth ---
        if parts == ["auth", "login"] and method == "POST":
            data = self._json_body()
            username = (data.get("username") or "").strip()
            password = data.get("password") or ""
            row = conn.execute("SELECT * FROM users WHERE username = ? AND active = 1", (username,)).fetchone()
            if not row:
                raise ApiError(401, "Incorrect username or password")
            calc, _ = hash_password(password, row["salt"])
            if not secrets.compare_digest(calc, row["password_hash"]):
                raise ApiError(401, "Incorrect username or password")
            token = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions (token,user_id,created_at) VALUES (?,?,?)",
                         (token, row["id"], now()))
            return 200, {"token": token, "user": public_user(row)}

        if parts == ["auth", "logout"] and method == "POST":
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                conn.execute("DELETE FROM sessions WHERE token = ?", (auth[7:],))
            return 200, {"ok": True}

        if parts == ["auth", "me"] and method == "GET":
            user = self._require(conn)
            return 200, {"user": public_user(user)}

        # --- modules (navigation registry) ---
        if parts == ["modules"] and method == "GET":
            user = self._require(conn)
            rows = conn.execute("SELECT * FROM modules WHERE enabled = 1 ORDER BY sort").fetchall()
            visible = [dict(r) for r in rows if r["min_role"] == "TECHNICIAN" or user["role"] == "ADMIN"]
            return 200, {"modules": visible}

        # --- technicians ---
        if parts == ["technicians"] and method == "GET":
            self._require(conn, "ADMIN")
            date = (query.get("date", [""])[0] or "").strip()
            rows = conn.execute(
                "SELECT u.id, u.username, u.display_name, r.code AS reason, "
                "(SELECT COUNT(*) FROM jobs j WHERE j.assignee_id = u.id AND j.status IN ('SCHEDULED','IN_PROGRESS')) AS job_count "
                "FROM users u LEFT JOIN roster r ON r.user_id = u.id AND r.on_date = ? "
                "WHERE u.role = 'TECHNICIAN' AND u.active = 1 ORDER BY u.display_name",
                (date,),
            ).fetchall()
            techs = []
            for r in rows:
                d = dict(r)
                d["available"] = d["reason"] is None
                techs.append(d)
            return 200, {"technicians": techs, "date": date}

        # --- site management dashboard (admin) ---
        if parts == ["dashboard"] and method == "GET":
            self._require(conn, "ADMIN")
            pend = conn.execute(
                "SELECT status, COUNT(*) c FROM pending_entries GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["c"] for r in pend}
            open_pendings = sum(c for s, c in by_status.items() if s != "ACTIONED")

            svc_rows = conn.execute(
                "SELECT a.tag, r.occurred_on FROM asset_records r JOIN assets a ON a.id = r.asset_id "
                "WHERE r.category = 'service' AND r.name LIKE '108 Month%' AND r.occurred_on IS NOT NULL"
            ).fetchall()
            services = sorted(
                ({"tag": r["tag"], "base_108mo": r["occurred_on"],
                  "due": add_months(r["occurred_on"], 6)} for r in svc_rows),
                key=lambda x: x["due"],
            )
            td = today()
            upcoming = [s for s in services if s["due"] >= td] or services
            next_services = upcoming[:10]

            retro_rows = conn.execute(
                "SELECT a.tag, r.name, r.status FROM asset_records r JOIN assets a ON a.id = r.asset_id "
                "WHERE r.category = 'retrofit' AND r.status IN ('outstanding','in_progress') "
                "ORDER BY r.name, a.tag"
            ).fetchall()
            retro_by_name = {}
            for r in retro_rows:
                retro_by_name.setdefault(r["name"], {"name": r["name"], "outstanding": [], "in_progress": []})
                retro_by_name[r["name"]][r["status"]].append(r["tag"])
            incomplete_retrofits = sorted(
                retro_by_name.values(),
                key=lambda g: -(len(g["outstanding"]) + len(g["in_progress"])),
            )

            return 200, {
                "open_pendings": open_pendings,
                "pendings_by_status": by_status,
                "next_services": next_services,
                "service_count": len(services),
                "incomplete_retrofits": incomplete_retrofits,
                "incomplete_retrofit_count": len(retro_rows),
            }

        # --- daily team plan ---
        if parts == ["plan"] and method == "GET":
            self._require(conn, "ADMIN")
            date = (query.get("date", [today()])[0] or today()).strip()
            return 200, load_plan(conn, date)

        if parts == ["plan"] and method == "POST":
            self._require(conn, "ADMIN")
            data = self._json_body()
            date = (data.get("date") or today()).strip()
            team_no = int(data.get("team_no") or 0)
            op = data.get("op")
            if not (1 <= team_no <= 10):
                raise ApiError(400, "team_no must be 1-10")
            conn.execute(
                "INSERT OR IGNORE INTO plan_team (plan_date, team_no, job_id) VALUES (?,?,NULL)",
                (date, team_no),
            )
            if op == "set_job":
                job_id = data.get("job_id")
                job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
                if not job:
                    raise ApiError(404, "Job not found")
                # free the job from any other team on this date
                conn.execute("UPDATE plan_team SET job_id = NULL WHERE plan_date = ? AND job_id = ?",
                             (date, job_id))
                conn.execute("UPDATE plan_team SET job_id = ? WHERE plan_date = ? AND team_no = ?",
                             (job_id, date, team_no))
                conn.execute("UPDATE jobs SET status = 'SCHEDULED', scheduled_date = ?, updated_at = ? "
                             "WHERE id = ? AND status = 'OUTSTANDING'", (date, now(), job_id))
            elif op == "clear_job":
                row = conn.execute("SELECT job_id FROM plan_team WHERE plan_date = ? AND team_no = ?",
                                   (date, team_no)).fetchone()
                jid = row["job_id"] if row else None
                conn.execute("UPDATE plan_team SET job_id = NULL WHERE plan_date = ? AND team_no = ?",
                             (date, team_no))
                if jid:
                    conn.execute("UPDATE jobs SET status = 'OUTSTANDING', scheduled_date = NULL, updated_at = ? "
                                 "WHERE id = ? AND status = 'SCHEDULED'", (now(), jid))
            elif op == "add_member":
                uid = data.get("user_id")
                u = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'TECHNICIAN'", (uid,)).fetchone()
                if not u:
                    raise ApiError(404, "Technician not found")
                off = conn.execute("SELECT code FROM roster WHERE user_id = ? AND on_date = ?",
                                   (uid, date)).fetchone()
                if off:
                    raise ApiError(409, "%s is unavailable (%s)" % (u["display_name"], off["code"]))
                # a technician belongs to at most one team per day
                conn.execute("DELETE FROM plan_member WHERE plan_date = ? AND user_id = ?", (date, uid))
                conn.execute("INSERT INTO plan_member (plan_date, team_no, user_id) VALUES (?,?,?)",
                             (date, team_no, uid))
            elif op == "remove_member":
                conn.execute("DELETE FROM plan_member WHERE plan_date = ? AND team_no = ? AND user_id = ?",
                             (date, team_no, data.get("user_id")))
            else:
                raise ApiError(400, "Unknown op")
            return 200, load_plan(conn, date)

        # --- assets ---
        if parts == ["assets"] and method == "GET":
            self._require(conn)
            rows = conn.execute(
                "SELECT a.*, "
                "(SELECT COUNT(*) FROM pending_entries p WHERE p.asset_id = a.id AND p.status != 'ACTIONED') AS open_pendings "
                "FROM assets a ORDER BY a.tag"
            ).fetchall()
            return 200, {"assets": [dict(r) for r in rows]}

        if len(parts) == 2 and parts[0] == "assets" and method == "GET":
            self._require(conn)
            asset = conn.execute("SELECT * FROM assets WHERE id = ?", (parts[1],)).fetchone()
            if not asset:
                raise ApiError(404, "Asset not found")
            jobs = conn.execute(
                "SELECT j.*, u.display_name AS assignee_name FROM jobs j "
                "LEFT JOIN users u ON u.id = j.assignee_id WHERE j.asset_id = ? ORDER BY j.created_at DESC",
                (parts[1],),
            ).fetchall()
            recs = conn.execute(
                "SELECT category, name, occurred_on AS date, detail, status FROM asset_records "
                "WHERE asset_id = ? ORDER BY sort, name", (parts[1],),
            ).fetchall()
            history = conn.execute(
                "SELECT occurred_on AS date, description, work_type, service_order, technicians "
                "FROM asset_history WHERE asset_id = ? "
                "ORDER BY occurred_on DESC, id DESC", (parts[1],),
            ).fetchall()
            services = [dict(r) for r in recs if r["category"] == "service"]
            s108 = next((s["date"] for s in services
                         if s["name"].startswith("108 Month") and s["date"]), None)
            return 200, {
                "asset": dict(asset),
                "jobs": [dict(r) for r in jobs],
                "services": services,
                "hv": [dict(r) for r in recs if r["category"] == "hv"],
                "stat": [dict(r) for r in recs if r["category"] == "stat"],
                "retrofits": [dict(r) for r in recs if r["category"] == "retrofit"],
                "components": [dict(r) for r in recs if r["category"] == "component"],
                "history": [dict(r) for r in history],
                "next_service": {
                    "base_108mo": s108,
                    "due": add_months(s108, 6) if s108 else None,
                },
            }

        if len(parts) == 3 and parts[0] == "assets" and parts[2] == "pendings":
            asset_id = parts[1]
            if method == "GET":
                self._require(conn)
                return 200, {"pendings": load_pendings(conn, asset_id=asset_id)}
            if method == "POST":
                user = self._require(conn)
                return self._create_pending(conn, asset_id, user)

        # --- pendings ---
        if parts == ["pendings", "export"] and method == "GET":
            self._require(conn, "ADMIN")
            rows = conn.execute(
                "SELECT a.tag AS turbine, p.wo_code, p.priority, p.system, p.status, "
                "u.display_name AS logged_by, p.created_at, p.note, "
                "(SELECT COUNT(*) FROM pending_photos ph WHERE ph.pending_entry_id = p.id) AS photos "
                "FROM pending_entries p "
                "JOIN assets a ON a.id = p.asset_id "
                "JOIN users u ON u.id = p.author_id "
                "ORDER BY a.tag, p.created_at, p.id"
            ).fetchall()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Turbine", "WO code", "Priority", "System", "Status",
                        "Logged by", "Date", "Note", "Photos"])
            for r in rows:
                w.writerow([
                    r["turbine"], r["wo_code"] or "",
                    r["priority"] if r["priority"] is not None else "",
                    r["system"] or "", r["status"], r["logged_by"],
                    (r["created_at"] or "")[:10],
                    (r["note"] or "").replace("\r\n", "\n"), r["photos"],
                ])
            return 200, RawResponse(buf.getvalue(), "text/csv; charset=utf-8",
                                    "pendings-%s.csv" % today())

        if len(parts) == 2 and parts[0] == "pendings" and method == "PATCH":
            self._require(conn, "ADMIN")
            data = self._json_body()
            status = data.get("status")
            if status not in ("SUBMITTED", "REVIEWED", "ACTIONED"):
                raise ApiError(400, "Invalid status")
            cur = conn.execute("UPDATE pending_entries SET status = ? WHERE id = ?", (status, parts[1]))
            if cur.rowcount == 0:
                raise ApiError(404, "Pending entry not found")
            return 200, {"ok": True}

        # --- jobs ---
        if parts == ["jobs"] and method == "GET":
            self._require(conn)
            where, args = [], []
            if "status" in query:
                where.append("j.status = ?"); args.append(query["status"][0])
            if query.get("unassigned", ["false"])[0] == "true":
                where.append("j.assignee_id IS NULL")
            if "assignee" in query:
                where.append("j.assignee_id = ?"); args.append(query["assignee"][0])
            sql = ("SELECT j.*, a.tag AS asset_tag, a.name AS asset_name, u.display_name AS assignee_name "
                   "FROM jobs j LEFT JOIN assets a ON a.id = j.asset_id "
                   "LEFT JOIN users u ON u.id = j.assignee_id")
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY CASE j.priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, j.due_date"
            rows = conn.execute(sql, args).fetchall()
            return 200, {"jobs": [dict(r) for r in rows]}

        if parts == ["jobs"] and method == "POST":
            user = self._require(conn, "ADMIN")
            data = self._json_body()
            title = (data.get("title") or "").strip()
            if not title:
                raise ApiError(400, "Title is required")
            cur = conn.execute(
                "INSERT INTO jobs (title,description,asset_id,priority,status,estimated_minutes,due_date,created_at,updated_at) "
                "VALUES (?,?,?,?,'OUTSTANDING',?,?,?,?)",
                (title, data.get("description", ""), data.get("asset_id"),
                 data.get("priority", "MEDIUM"), data.get("estimated_minutes"),
                 data.get("due_date"), now(), now()),
            )
            log_job(conn, cur.lastrowid, user["id"], "Job created")
            return 201, {"id": cur.lastrowid}

        if len(parts) == 2 and parts[0] == "jobs" and method == "PATCH":
            user = self._require(conn, "ADMIN")
            job = conn.execute("SELECT * FROM jobs WHERE id = ?", (parts[1],)).fetchone()
            if not job:
                raise ApiError(404, "Job not found")
            data = self._json_body()
            sets, args, notes = [], [], []

            if "assignee_id" in data:
                new_assignee = data["assignee_id"]
                sets.append("assignee_id = ?"); args.append(new_assignee)
                if new_assignee:
                    who = conn.execute("SELECT display_name FROM users WHERE id = ?", (new_assignee,)).fetchone()
                    if not who:
                        raise ApiError(400, "Unknown technician")
                    notes.append("Assigned to %s" % who["display_name"])
                    if job["status"] == "OUTSTANDING":
                        sets.append("status = ?"); args.append("SCHEDULED")
                else:
                    notes.append("Unassigned - returned to backlog")
                    sets.append("status = ?"); args.append("OUTSTANDING")
                    sets.append("scheduled_date = ?"); args.append(None)

            if "scheduled_date" in data:
                sets.append("scheduled_date = ?"); args.append(data["scheduled_date"])
                notes.append("Scheduled for %s" % data["scheduled_date"])

            if "status" in data:
                st = data["status"]
                if st not in ("OUTSTANDING", "SCHEDULED", "IN_PROGRESS", "COMPLETE", "CANCELLED"):
                    raise ApiError(400, "Invalid status")
                sets.append("status = ?"); args.append(st)
                notes.append("Status -> %s" % st)

            if "priority" in data:
                sets.append("priority = ?"); args.append(data["priority"])

            if not sets:
                raise ApiError(400, "Nothing to update")
            sets.append("updated_at = ?"); args.append(now())
            args.append(parts[1])
            conn.execute("UPDATE jobs SET %s WHERE id = ?" % ", ".join(sets), args)
            for n in notes:
                log_job(conn, job["id"], user["id"], n)
            updated = conn.execute(
                "SELECT j.*, a.tag AS asset_tag, a.name AS asset_name, u.display_name AS assignee_name "
                "FROM jobs j LEFT JOIN assets a ON a.id=j.asset_id LEFT JOIN users u ON u.id=j.assignee_id "
                "WHERE j.id = ?", (parts[1],),
            ).fetchone()
            return 200, {"job": dict(updated)}

        return None

    def _create_pending(self, conn, asset_id, user):
        asset = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            raise ApiError(404, "Asset not found")
        ctype = self.headers.get("Content-Type", "")
        note, files = "", []
        if ctype.startswith("multipart/form-data"):
            m = re.search(r"boundary=(.+)$", ctype)
            if not m:
                raise ApiError(400, "Missing multipart boundary")
            fields, files = parse_multipart(self._body(), m.group(1).strip('"'))
            note = (fields.get("note") or "").strip()
        else:
            note = (self._json_body().get("note") or "").strip()
        if not note:
            raise ApiError(400, "A note is required")

        cur = conn.execute(
            "INSERT INTO pending_entries (asset_id,author_id,note,status,created_at) VALUES (?,?,?,'SUBMITTED',?)",
            (asset_id, user["id"], note, now()),
        )
        entry_id = cur.lastrowid
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        saved = 0
        for f in files:
            if not f["content"] or saved >= 8:
                continue
            ext = os.path.splitext(f["filename"])[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"):
                ext = ".jpg"
            fname = "p%d_%s%s" % (entry_id, secrets.token_hex(6), ext)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
                out.write(f["content"][:15 * 1024 * 1024])
            conn.execute(
                "INSERT INTO pending_photos (pending_entry_id,filename,caption,created_at) VALUES (?,?,?,?)",
                (entry_id, fname, "", now()),
            )
            saved += 1
        return 201, {"id": entry_id, "photos": saved}

    # -- static files ----------------------------------------------------

    def _serve_upload(self, name):
        name = os.path.basename(name)
        full = os.path.join(UPLOAD_DIR, name)
        if not os.path.isfile(full):
            raise ApiError(404, "Not found")
        self._send_file(full)

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            # SPA fallback
            full = os.path.join(WEB_DIR, "index.html")
        self._send_file(full)

    def _send_file(self, full):
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)


def public_user(row):
    return {"id": row["id"], "username": row["username"],
            "display_name": row["display_name"], "role": row["role"]}


def log_job(conn, job_id, user_id, message):
    conn.execute("INSERT INTO job_activity (job_id,user_id,message,created_at) VALUES (?,?,?,?)",
                 (job_id, user_id, message, now()))


def load_plan(conn, date):
    """Return the 10-team day plan plus roster + backlog for `date`."""
    tech_rows = conn.execute(
        "SELECT u.id, u.display_name, r.code AS reason "
        "FROM users u LEFT JOIN roster r ON r.user_id = u.id AND r.on_date = ? "
        "WHERE u.role = 'TECHNICIAN' AND u.active = 1 ORDER BY u.display_name",
        (date,),
    ).fetchall()
    techs = [{"id": r["id"], "display_name": r["display_name"],
              "reason": r["reason"], "available": r["reason"] is None} for r in tech_rows]

    members = conn.execute(
        "SELECT m.team_no, u.id, u.display_name FROM plan_member m "
        "JOIN users u ON u.id = m.user_id WHERE m.plan_date = ? ORDER BY u.display_name",
        (date,),
    ).fetchall()
    by_team = {}
    for m in members:
        by_team.setdefault(m["team_no"], []).append({"id": m["id"], "display_name": m["display_name"]})

    placed = conn.execute(
        "SELECT t.team_no, j.id, j.title, j.priority, j.estimated_minutes, j.due_date, "
        "a.tag AS asset_tag FROM plan_team t JOIN jobs j ON j.id = t.job_id "
        "LEFT JOIN assets a ON a.id = j.asset_id WHERE t.plan_date = ?",
        (date,),
    ).fetchall()
    job_by_team = {p["team_no"]: dict(p) for p in placed}
    placed_ids = {p["id"] for p in placed}

    teams = []
    for n in range(1, 11):
        teams.append({
            "team_no": n,
            "job": job_by_team.get(n),
            "members": by_team.get(n, []),
        })

    backlog = conn.execute(
        "SELECT j.id, j.title, j.priority, j.estimated_minutes, j.due_date, a.tag AS asset_tag "
        "FROM jobs j LEFT JOIN assets a ON a.id = j.asset_id "
        "WHERE j.status IN ('OUTSTANDING','SCHEDULED') "
        "ORDER BY CASE j.priority WHEN 'URGENT' THEN 0 WHEN 'HIGH' THEN 1 WHEN 'MEDIUM' THEN 2 ELSE 3 END, j.due_date"
    ).fetchall()
    backlog = [dict(j) for j in backlog if j["id"] not in placed_ids]

    assigned_ids = {m["id"] for m in members}
    return {
        "date": date,
        "teams": teams,
        "backlog": backlog,
        "available": [t for t in techs if t["available"] and t["id"] not in assigned_ids],
        "unavailable": [t for t in techs if not t["available"]],
        "assigned_count": len(assigned_ids),
    }


def load_pendings(conn, asset_id):
    rows = conn.execute(
        "SELECT p.*, u.display_name AS author_name FROM pending_entries p "
        "JOIN users u ON u.id = p.author_id WHERE p.asset_id = ? ORDER BY p.created_at DESC",
        (asset_id,),
    ).fetchall()
    out = []
    for r in rows:
        photos = conn.execute(
            "SELECT id, filename, caption FROM pending_photos WHERE pending_entry_id = ? ORDER BY id",
            (r["id"],),
        ).fetchall()
        d = dict(r)
        d["photos"] = [{"id": p["id"], "url": "/uploads/" + p["filename"], "caption": p["caption"]}
                       for p in photos]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="wipe data and reseed")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print("  data reset")
    seed()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port)
    print("\n  Site Portal")
    print("  " + "-" * 40)
    print("  running at   %s" % url)
    print("  admin login  admin / admin123")
    print("  technician   sclydesdale / tech123   (24 Manplan techs, all tech123)")
    print("  stop         Ctrl-C\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
