#!/usr/bin/env python3
"""
Role-based Site Portal app.
Single-file server: Python standard library only (no pip installs, no network).

  python3 app.py            # serves http://localhost:8000
  python3 app.py --port 9000
  python3 app.py --reset    # wipe data + reseed

Roles & accounts:
  All logins come from source/Credentials.csv (Name, First name, Username, Access, Password),
  re-synced on every start. Access=Admin -> ADMIN, otherwise -> TECHNICIAN.
    ADMIN       full edit rights (records, pendings review/parts, planning, Data Explorer)
    TECHNICIAN  Site Dashboard + Asset Information, read-only except add/complete pendings
  A built-in `admin` / `admin123` account is always kept (override with $ADMIN_PASSWORD).
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
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import seed_data

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
# $DATA_DIR lets a host point the SQLite DB + uploads at a persistent disk.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
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

-- Audit log of every edit to an asset_records row (drives the Data Explorer change report).
CREATE TABLE IF NOT EXISTS record_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id   INTEGER NOT NULL,
    asset_id    INTEGER NOT NULL REFERENCES assets(id),
    category    TEXT NOT NULL,
    record_name TEXT NOT NULL,
    field       TEXT NOT NULL,          -- 'occurred_on' | 'detail' | 'status'
    old_value   TEXT,
    new_value   TEXT,
    changed_by  INTEGER REFERENCES users(id),
    changed_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_record_changes_at ON record_changes(changed_at);

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

-- status flows Submitted -> Reviewed -> Completed
CREATE TABLE IF NOT EXISTS pending_entries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id   INTEGER NOT NULL REFERENCES assets(id),
    author_id  INTEGER NOT NULL REFERENCES users(id),
    note       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'SUBMITTED',
    wo_code    TEXT,                  -- SAP/SGRE notification number (imported from Pendings)
    priority   INTEGER,               -- SGRE priority (1 highest .. 6 lowest)
    system     TEXT,                  -- affected turbine system
    -- parts reservation (added by an admin while status = REVIEWED)
    parts_service_order TEXT,
    parts_reserved_at   TEXT,
    -- completion evidence (added by a technician to move REVIEWED -> COMPLETED)
    completed_note TEXT,
    completed_by   INTEGER REFERENCES users(id),
    completed_at   TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_parts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_entry_id  INTEGER NOT NULL REFERENCES pending_entries(id),
    part_number       TEXT NOT NULL,
    quantity          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_photos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_entry_id  INTEGER NOT NULL REFERENCES pending_entries(id),
    filename          TEXT NOT NULL,
    caption           TEXT,
    kind              TEXT NOT NULL DEFAULT 'note',   -- 'note' | 'evidence'
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

    # --- users: synced from Credentials.csv on every start (+ an `admin` break-glass) ---
    sync_users(conn, data["credentials"])

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
        add_records(data["blades"], "blade")

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

    # navigation registry — re-synced every start so role changes take effect.
    #   technicians: Site Dashboard + Asset Information   admins: + Planning + Data Explorer
    for key, name, min_role, sort in [
        ("dashboard", "Site Dashboard", "TECHNICIAN", 5),
        ("assets", "Asset Information", "TECHNICIAN", 10),
        ("planning", "Planning", "ADMIN", 20),
        ("explorer", "Data Explorer", "ADMIN", 30),
    ]:
        if conn.execute("SELECT 1 FROM modules WHERE key = ?", (key,)).fetchone():
            conn.execute("UPDATE modules SET name=?, min_role=?, sort=?, enabled=1 WHERE key=?",
                         (name, min_role, sort, key))
        else:
            conn.execute("INSERT INTO modules (key,name,enabled,min_role,sort) VALUES (?,?,1,?,?)",
                         (key, name, min_role, sort))

    conn.commit()
    conn.close()


def sync_users(conn, creds):
    """Create/refresh login accounts from Credentials.csv. Runs on every start.

    Keeps an `admin` break-glass account (password overridable with $ADMIN_PASSWORD).
    Anyone not in the credentials list is deactivated, not deleted, so history and
    roster/plan references stay intact.
    """
    existing = {r["username"]: r for r in conn.execute("SELECT * FROM users")}
    keep = set()

    def upsert(username, password, role, name):
        keep.add(username)
        cur = existing.get(username)
        if cur:
            calc, _ = hash_password(password, cur["salt"])
            pw_ok = secrets.compare_digest(calc, cur["password_hash"])
            if pw_ok and cur["role"] == role and cur["display_name"] == name and cur["active"]:
                return
            if pw_ok:
                conn.execute("UPDATE users SET role=?, display_name=?, active=1 WHERE username=?",
                             (role, name, username))
            else:
                ph, salt = hash_password(password)
                conn.execute("UPDATE users SET password_hash=?, salt=?, role=?, display_name=?, "
                             "active=1 WHERE username=?", (ph, salt, role, name, username))
        else:
            ph, salt = hash_password(password)
            conn.execute("INSERT INTO users (username,password_hash,salt,role,display_name,active,created_at) "
                         "VALUES (?,?,?,?,?,1,?)", (username, ph, salt, role, name, now()))

    upsert("admin", os.environ.get("ADMIN_PASSWORD") or "admin123", "ADMIN", "Site Administrator")
    for c in creds:
        upsert(c["username"], c["password"], c["role"], c["name"])

    for uname, row in existing.items():
        if uname not in keep and row["active"]:
            conn.execute("UPDATE users SET active=0 WHERE username=?", (uname,))


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
# Data Explorer: bulk view/edit of asset_records + change reporting
# ---------------------------------------------------------------------------

# (key, label, editable value field on asset_records)
EXPLORER_CATEGORIES = [
    ("service",   "Service dates", "occurred_on"),
    ("hv",        "HV history",    "occurred_on"),
    ("stat",      "Stat history",  "occurred_on"),
    ("retrofit",  "Retrofits",     "occurred_on"),
    ("blade",     "Blades",        "occurred_on"),
    ("component", "Components",     "detail"),
]
EXPLORER_LABEL = {k: lbl for k, lbl, _ in EXPLORER_CATEGORIES}
EXPLORER_VALUE_FIELD = {k: vf for k, _l, vf in EXPLORER_CATEGORIES}


def explorer_matrix(conn, category, value_field):
    """Return (columns, {tag: {name: {id, value, status}}}) for one category."""
    cols = [r["name"] for r in conn.execute(
        "SELECT name, MIN(sort) s FROM asset_records WHERE category = ? "
        "GROUP BY name ORDER BY s, name", (category,))]
    recs = conn.execute(
        "SELECT r.id, a.tag, r.name, r.occurred_on, r.detail, r.status "
        "FROM asset_records r JOIN assets a ON a.id = r.asset_id "
        "WHERE r.category = ? ORDER BY a.tag", (category,)).fetchall()
    by_tag = {}
    for r in recs:
        by_tag.setdefault(r["tag"], {})[r["name"]] = {
            "id": r["id"],
            "value": r["occurred_on"] if value_field == "occurred_on" else r["detail"],
            "status": r["status"],
        }
    return cols, by_tag


def log_record_change(conn, rec, field, old, new, user_id):
    conn.execute(
        "INSERT INTO record_changes "
        "(record_id, asset_id, category, record_name, field, old_value, new_value, changed_by, changed_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rec["id"], rec["asset_id"], rec["category"], rec["name"], field,
         old, new, user_id, now()),
    )


def _col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _xml_escape(v):
    return (str(v).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_xlsx(sheets):
    """sheets = [(name, rows)], rows = [[cell, ...]]. Returns .xlsx bytes (stdlib only)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
              '<Default Extension="xml" ContentType="application/xml"/>',
              '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
        for i in range(len(sheets)):
            ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" '
                      'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
        ct.append('</Types>')
        z.writestr("[Content_Types].xml", "".join(ct))

        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
                   'Target="xl/workbook.xml"/></Relationships>')

        sheet_tags, rel_tags = [], []
        for i, (name, _rows) in enumerate(sheets):
            sid = i + 1
            safe = _xml_escape(re.sub(r'[\[\]:*?/\\]', " ", name))[:31]
            sheet_tags.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (safe, sid, sid))
            rel_tags.append('<Relationship Id="rId%d" '
                            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                            'Target="worksheets/sheet%d.xml"/>' % (sid, sid))
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets>' + "".join(sheet_tags) + '</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(rel_tags) + '</Relationships>')

        for i, (_name, rows) in enumerate(sheets):
            xml = []
            for r_idx, row in enumerate(rows, start=1):
                cells = []
                for c_idx, val in enumerate(row, start=1):
                    if val is None or val == "":
                        continue
                    cells.append('<c r="%s%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                                 % (_col_letter(c_idx), r_idx, _xml_escape(val)))
                xml.append('<row r="%d">%s</row>' % (r_idx, "".join(cells)))
            z.writestr("xl/worksheets/sheet%d.xml" % (i + 1),
                       '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                       '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                       '<sheetData>' + "".join(xml) + '</sheetData></worksheet>')
    return buf.getvalue()


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

        # --- site dashboard (any signed-in user; read-only figures) ---
        if parts == ["dashboard"] and method == "GET":
            self._require(conn)
            by_status = {r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) c FROM pending_entries GROUP BY status")}
            open_pendings = sum(c for s, c in by_status.items() if s != "COMPLETED")

            services = service_due_list(conn)
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
                "next_services": services,
                "service_count": len(services),
                "incomplete_retrofits": incomplete_retrofits,
                "incomplete_retrofit_count": len(retro_rows),
            }

        # --- all pending entries (dashboard drill-down list; any signed-in user) ---
        if parts == ["pendings"] and method == "GET":
            self._require(conn)
            want = (query.get("status", [""])[0] or "").upper()
            sql = ("SELECT p.*, a.tag AS turbine, u.display_name AS author_name, "
                   "cu.display_name AS completed_by_name "
                   "FROM pending_entries p JOIN assets a ON a.id = p.asset_id "
                   "JOIN users u ON u.id = p.author_id "
                   "LEFT JOIN users cu ON cu.id = p.completed_by")
            args = []
            if want in ("SUBMITTED", "REVIEWED", "COMPLETED"):
                sql += " WHERE p.status = ?"
                args.append(want)
            sql += (" ORDER BY CASE p.status WHEN 'SUBMITTED' THEN 0 WHEN 'REVIEWED' THEN 1 ELSE 2 END, "
                    "p.priority IS NULL, p.priority, a.tag")
            rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
            by_id = {r["id"]: r for r in rows}
            for r in rows:
                r["parts"], r["photos"] = [], []
            for pt in conn.execute("SELECT pending_entry_id, part_number, quantity FROM pending_parts"):
                if pt["pending_entry_id"] in by_id:
                    by_id[pt["pending_entry_id"]]["parts"].append(
                        {"part_number": pt["part_number"], "quantity": pt["quantity"]})
            for ph in conn.execute("SELECT pending_entry_id, filename, kind FROM pending_photos"):
                if ph["pending_entry_id"] in by_id:
                    by_id[ph["pending_entry_id"]]["photos"].append(
                        {"url": "/uploads/" + ph["filename"], "kind": ph["kind"]})
            counts = {s: 0 for s in ("SUBMITTED", "REVIEWED", "COMPLETED")}
            for r in conn.execute("SELECT status, COUNT(*) c FROM pending_entries GROUP BY status"):
                counts[r["status"]] = r["c"]
            return 200, {"pendings": rows, "counts": counts}

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
                "(SELECT COUNT(*) FROM pending_entries p WHERE p.asset_id = a.id AND p.status != 'COMPLETED') AS open_pendings "
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
                "SELECT id, category, name, occurred_on AS date, detail, status, sort FROM asset_records "
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
            ordered = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY tag")]
            tag_by_id = {r["id"]: r["tag"] for r in conn.execute("SELECT id, tag FROM assets")}
            idx = ordered.index(asset["id"])
            prev_id = ordered[idx - 1] if idx > 0 else ordered[-1]
            next_id = ordered[(idx + 1) % len(ordered)]
            return 200, {
                "asset": dict(asset),
                "prev": {"id": prev_id, "tag": tag_by_id[prev_id]},
                "next": {"id": next_id, "tag": tag_by_id[next_id]},
                "jobs": [dict(r) for r in jobs],
                "services": services,
                "hv": [dict(r) for r in recs if r["category"] == "hv"],
                "stat": [dict(r) for r in recs if r["category"] == "stat"],
                "retrofits": [dict(r) for r in recs if r["category"] == "retrofit"],
                "components": [dict(r) for r in recs if r["category"] == "component"],
                "blades": [dict(r) for r in recs if r["category"] == "blade"],
                "history": [dict(r) for r in history],
                "next_service": {
                    "base_108mo": s108,
                    "due": add_months(s108, 6) if s108 else None,
                },
            }

        # set / clear a service, blade, hv or retrofit record date (admin only)
        if len(parts) == 2 and parts[0] == "records" and method == "PATCH":
            user = self._require(conn, "ADMIN")
            rec = conn.execute("SELECT * FROM asset_records WHERE id = ?", (parts[1],)).fetchone()
            if not rec:
                raise ApiError(404, "Record not found")
            if rec["category"] not in ("service", "blade", "hv", "retrofit"):
                raise ApiError(403, "This record type is not editable")
            raw = (self._json_body().get("occurred_on") or "").strip()
            iso = None
            if raw:
                m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
                if not m:
                    raise ApiError(400, "Date must be YYYY-MM-DD")
                y = int(m.group(1))
                if not (2005 <= y <= 2040):
                    raise ApiError(400, "Date out of range")
                iso = raw
            old = rec["occurred_on"]
            if rec["category"] == "retrofit":
                # entering a date completes the retrofit; clearing it reopens it
                new_status = "complete" if iso else "outstanding"
                conn.execute("UPDATE asset_records SET occurred_on = ?, status = ? WHERE id = ?",
                             (iso, new_status, parts[1]))
                if (old or None) != (iso or None):
                    log_record_change(conn, rec, "occurred_on", old, iso, user["id"])
                if rec["status"] != new_status:
                    log_record_change(conn, rec, "status", rec["status"], new_status, user["id"])
                return 200, {"ok": True, "occurred_on": iso, "status": new_status}
            conn.execute("UPDATE asset_records SET occurred_on = ? WHERE id = ?", (iso, parts[1]))
            if (old or None) != (iso or None):
                log_record_change(conn, rec, "occurred_on", old, iso, user["id"])
            return 200, {"ok": True, "occurred_on": iso}

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
            self._require(conn)
            want = (query.get("status", [""])[0] or "").upper()
            sql = (
                "SELECT a.tag AS turbine, p.wo_code, p.priority, p.system, p.status, "
                "u.display_name AS logged_by, p.created_at, p.note, p.parts_service_order, "
                "cu.display_name AS completed_by_name, p.completed_at, p.completed_note, "
                "(SELECT COUNT(*) FROM pending_photos ph WHERE ph.pending_entry_id = p.id) AS photos, "
                "(SELECT group_concat(part_number || ' x' || quantity, '; ') "
                " FROM pending_parts pp WHERE pp.pending_entry_id = p.id) AS parts "
                "FROM pending_entries p "
                "JOIN assets a ON a.id = p.asset_id "
                "JOIN users u ON u.id = p.author_id "
                "LEFT JOIN users cu ON cu.id = p.completed_by"
            )
            args = []
            if want in ("SUBMITTED", "REVIEWED", "COMPLETED"):
                sql += " WHERE p.status = ?"
                args.append(want)
            sql += " ORDER BY a.tag, p.created_at, p.id"
            rows = conn.execute(sql, args).fetchall()
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Turbine", "WO code", "Priority", "System", "Status", "Logged by",
                        "Date", "Note", "Parts SO", "Parts", "Completed by",
                        "Completed", "Completion note", "Photos"])
            for r in rows:
                w.writerow([
                    r["turbine"], r["wo_code"] or "",
                    r["priority"] if r["priority"] is not None else "",
                    r["system"] or "", r["status"], r["logged_by"],
                    (r["created_at"] or "")[:10],
                    (r["note"] or "").replace("\r\n", "\n"),
                    r["parts_service_order"] or "", r["parts"] or "",
                    r["completed_by_name"] or "", (r["completed_at"] or "")[:10],
                    (r["completed_note"] or "").replace("\r\n", "\n"), r["photos"],
                ])
            fname = "pendings-%s%s.csv" % (
                (want.lower() + "-") if want in ("SUBMITTED", "REVIEWED", "COMPLETED") else "",
                today())
            return 200, RawResponse(buf.getvalue(), "text/csv; charset=utf-8", fname)

        # admin sets Submitted <-> Reviewed (and can reopen a Completed entry)
        if len(parts) == 2 and parts[0] == "pendings" and method == "PATCH":
            self._require(conn, "ADMIN")
            row = conn.execute("SELECT status FROM pending_entries WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "Pending entry not found")
            status = (self._json_body().get("status") or "").upper()
            if status not in ("SUBMITTED", "REVIEWED"):
                raise ApiError(400, "Admin can set Submitted or Reviewed. "
                                    "Completion is done by a technician with evidence.")
            conn.execute("UPDATE pending_entries SET status = ? WHERE id = ?", (status, parts[1]))
            if status == "SUBMITTED":  # leaving Reviewed drops any parts reservation
                conn.execute("DELETE FROM pending_parts WHERE pending_entry_id = ?", (parts[1],))
                conn.execute("UPDATE pending_entries SET parts_service_order = NULL, "
                             "parts_reserved_at = NULL WHERE id = ?", (parts[1],))
            return 200, {"ok": True}

        # admin reserves parts while an entry is Reviewed
        if len(parts) == 3 and parts[0] == "pendings" and parts[2] == "parts" and method == "POST":
            self._require(conn, "ADMIN")
            row = conn.execute("SELECT status FROM pending_entries WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "Pending entry not found")
            if row["status"] != "REVIEWED":
                raise ApiError(409, "Parts can only be reserved while the entry is Reviewed")
            data = self._json_body()
            so = (data.get("service_order") or "").strip()
            rows = []
            for p in (data.get("parts") or []):
                pn = str(p.get("part_number") or "").strip()
                qty = str(p.get("quantity") or "").strip() or "1"
                if pn:
                    rows.append((pn, qty))
            if not rows:
                raise ApiError(400, "At least one part number is required")
            conn.execute("DELETE FROM pending_parts WHERE pending_entry_id = ?", (parts[1],))
            for pn, qty in rows:
                conn.execute("INSERT INTO pending_parts (pending_entry_id,part_number,quantity) "
                             "VALUES (?,?,?)", (parts[1], pn, qty))
            conn.execute("UPDATE pending_entries SET parts_service_order = ?, parts_reserved_at = ? "
                         "WHERE id = ?", (so or None, now(), parts[1]))
            return 200, {"ok": True}

        # technician (or admin) completes a Reviewed entry with mandatory evidence
        if len(parts) == 3 and parts[0] == "pendings" and parts[2] == "complete" and method == "POST":
            user = self._require(conn)
            row = conn.execute("SELECT status FROM pending_entries WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "Pending entry not found")
            if row["status"] != "REVIEWED":
                raise ApiError(409, "Only a Reviewed entry can be completed")
            fields, files = self._multipart()
            if fields is None:
                raise ApiError(400, "Expected a multipart form (comment + photo)")
            comment = (fields.get("comment") or "").strip()
            if not comment:
                raise ApiError(400, "A completion comment is required")
            if not any(f["content"] for f in (files or [])):
                raise ApiError(400, "At least one evidence photo is required")
            self._save_photos(conn, int(parts[1]), files, "evidence")
            conn.execute(
                "UPDATE pending_entries SET status = 'COMPLETED', completed_note = ?, "
                "completed_by = ?, completed_at = ? WHERE id = ?",
                (comment, user["id"], now(), parts[1]),
            )
            return 200, {"ok": True}

        # --- data explorer (admin) ---
        if parts == ["explorer", "categories"] and method == "GET":
            self._require(conn, "ADMIN")
            return 200, {"categories": [
                {"key": k, "label": lbl, "value_field": vf}
                for k, lbl, vf in EXPLORER_CATEGORIES]}

        if parts == ["explorer", "matrix"] and method == "GET":
            self._require(conn, "ADMIN")
            cat = (query.get("category", [""])[0] or "").strip()
            if cat not in EXPLORER_VALUE_FIELD:
                raise ApiError(404, "Unknown category")
            vf = EXPLORER_VALUE_FIELD[cat]
            cols, by_tag = explorer_matrix(conn, cat, vf)
            rows = [{"tag": tag, "cells": [by_tag[tag].get(n) for n in cols]}
                    for tag in sorted(by_tag)]
            return 200, {"category": cat, "label": EXPLORER_LABEL[cat],
                         "value_field": vf, "columns": cols, "rows": rows}

        if parts == ["explorer", "export"] and method == "GET":
            self._require(conn, "ADMIN")
            cat = (query.get("category", [""])[0] or "").strip()
            if cat not in EXPLORER_VALUE_FIELD:
                raise ApiError(404, "Unknown category")
            vf = EXPLORER_VALUE_FIELD[cat]
            cols, by_tag = explorer_matrix(conn, cat, vf)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["Turbine"] + cols)
            for tag in sorted(by_tag):
                line = [tag]
                for n in cols:
                    cell = by_tag[tag].get(n)
                    # blank in the database -> blank in the export
                    line.append((cell or {}).get("value") or "")
                w.writerow(line)
            fname = "explorer-%s-%s.csv" % (cat, today())
            return 200, RawResponse(buf.getvalue(), "text/csv; charset=utf-8", fname)

        if parts == ["explorer", "apply"] and method == "POST":
            user = self._require(conn, "ADMIN")
            changes = self._json_body().get("changes") or []
            applied, errors = 0, []
            for ch in changes:
                rid = ch.get("id")
                rec = conn.execute("SELECT * FROM asset_records WHERE id = ?", (rid,)).fetchone()
                if not rec:
                    errors.append({"id": rid, "error": "Record not found"}); continue
                if rec["category"] not in EXPLORER_VALUE_FIELD:
                    errors.append({"id": rid, "error": "This record is not editable"}); continue
                vf = EXPLORER_VALUE_FIELD[rec["category"]]
                raw = ch.get("value")
                raw = "" if raw is None else str(raw).strip()
                if vf == "occurred_on":
                    iso = None
                    if raw:
                        if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
                            errors.append({"id": rid, "error": "Date must be YYYY-MM-DD"}); continue
                        if not (2005 <= int(raw[:4]) <= 2040):
                            errors.append({"id": rid, "error": "Date out of range"}); continue
                        iso = raw
                    old = rec["occurred_on"]
                    if (old or None) == (iso or None):
                        continue
                    if rec["category"] == "retrofit":
                        new_status = "complete" if iso else "outstanding"
                        conn.execute("UPDATE asset_records SET occurred_on=?, status=? WHERE id=?",
                                     (iso, new_status, rid))
                        if rec["status"] != new_status:
                            log_record_change(conn, rec, "status", rec["status"], new_status, user["id"])
                    else:
                        conn.execute("UPDATE asset_records SET occurred_on=? WHERE id=?", (iso, rid))
                    log_record_change(conn, rec, "occurred_on", old, iso, user["id"])
                    applied += 1
                else:
                    new = raw or None
                    old = rec["detail"]
                    if (old or None) == (new or None):
                        continue
                    conn.execute("UPDATE asset_records SET detail=? WHERE id=?", (new, rid))
                    log_record_change(conn, rec, "detail", old, new, user["id"])
                    applied += 1
            return 200, {"applied": applied, "errors": errors}

        if parts == ["explorer", "changes"] and method == "GET":
            self._require(conn, "ADMIN")
            frm = (query.get("from", [""])[0] or "").strip()
            to = (query.get("to", [""])[0] or "").strip()
            for d in (frm, to):
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    raise ApiError(400, "from and to dates are required (YYYY-MM-DD)")
            if frm > to:
                frm, to = to, frm
            FIELD_LABEL = {"occurred_on": "Date", "detail": "Detail", "status": "Status"}
            crows = conn.execute(
                "SELECT c.*, a.tag, u.display_name AS by_name FROM record_changes c "
                "JOIN assets a ON a.id = c.asset_id "
                "LEFT JOIN users u ON u.id = c.changed_by "
                "WHERE substr(c.changed_at,1,10) BETWEEN ? AND ? "
                "ORDER BY c.category, a.tag, c.changed_at", (frm, to)).fetchall()
            by_cat = {}
            for r in crows:
                by_cat.setdefault(r["category"], []).append(r)
            sheets = []
            for key, label, _vf in EXPLORER_CATEGORIES:
                rs = by_cat.get(key)
                if not rs:
                    continue
                data = [["Turbine", "Record", "Field", "Previous value",
                         "New value", "Changed by", "Changed at (UTC)"]]
                for r in rs:
                    data.append([
                        r["tag"], r["record_name"],
                        FIELD_LABEL.get(r["field"], r["field"]),
                        r["old_value"] or "", r["new_value"] or "",
                        r["by_name"] or "",
                        (r["changed_at"] or "").replace("T", " ").rstrip("Z"),
                    ])
                sheets.append((label, data))
            prows = conn.execute(
                "SELECT a.tag, p.wo_code, p.status, p.note, p.created_at, "
                "p.parts_reserved_at, p.completed_at, cu.display_name AS completed_by_name "
                "FROM pending_entries p JOIN assets a ON a.id = p.asset_id "
                "LEFT JOIN users cu ON cu.id = p.completed_by "
                "WHERE substr(p.created_at,1,10) BETWEEN ? AND ? "
                "   OR substr(COALESCE(p.parts_reserved_at,''),1,10) BETWEEN ? AND ? "
                "   OR substr(COALESCE(p.completed_at,''),1,10) BETWEEN ? AND ? "
                "ORDER BY a.tag, p.created_at",
                (frm, to, frm, to, frm, to)).fetchall()
            if prows:
                data = [["Turbine", "WO code", "Status", "Created", "Parts reserved",
                         "Completed", "Completed by", "Note"]]
                for r in prows:
                    data.append([
                        r["tag"], r["wo_code"] or "", r["status"],
                        (r["created_at"] or "")[:10], (r["parts_reserved_at"] or "")[:10],
                        (r["completed_at"] or "")[:10], r["completed_by_name"] or "",
                        (r["note"] or "").replace("\r\n", " ").replace("\n", " "),
                    ])
                sheets.append(("Pendings", data))
            if not sheets:
                raise ApiError(404, "No changes recorded between %s and %s" % (frm, to))
            fname = "change-report-%s_to_%s.xlsx" % (frm, to)
            return 200, RawResponse(
                build_xlsx(sheets),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                fname)

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

    def _multipart(self):
        ctype = self.headers.get("Content-Type", "")
        if not ctype.startswith("multipart/form-data"):
            return None, None
        m = re.search(r"boundary=(.+)$", ctype)
        if not m:
            raise ApiError(400, "Missing multipart boundary")
        return parse_multipart(self._body(), m.group(1).strip('"'))

    def _save_photos(self, conn, entry_id, files, kind="note", limit=8):
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        saved = 0
        for f in files or []:
            if not f["content"] or saved >= limit:
                continue
            ext = os.path.splitext(f["filename"])[1].lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic"):
                ext = ".jpg"
            fname = "p%d_%s%s" % (entry_id, secrets.token_hex(6), ext)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
                out.write(f["content"][:15 * 1024 * 1024])
            conn.execute(
                "INSERT INTO pending_photos (pending_entry_id,filename,caption,kind,created_at) "
                "VALUES (?,?,?,?,?)", (entry_id, fname, "", kind, now()),
            )
            saved += 1
        return saved

    def _create_pending(self, conn, asset_id, user):
        asset = conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not asset:
            raise ApiError(404, "Asset not found")
        fields, files = self._multipart()
        if fields is not None:
            note = (fields.get("note") or "").strip()
        else:
            note = (self._json_body().get("note") or "").strip()
        if not note:
            raise ApiError(400, "A note is required")
        cur = conn.execute(
            "INSERT INTO pending_entries (asset_id,author_id,note,status,created_at) "
            "VALUES (?,?,?,'SUBMITTED',?)", (asset_id, user["id"], note, now()),
        )
        saved = self._save_photos(conn, cur.lastrowid, files, "note")
        return 201, {"id": cur.lastrowid, "photos": saved}

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


def service_due_list(conn):
    """Every turbine with a 108-month completion date -> next service due (+6 months)."""
    rows = conn.execute(
        "SELECT a.tag, r.occurred_on FROM asset_records r JOIN assets a ON a.id = r.asset_id "
        "WHERE r.category = 'service' AND r.name LIKE '108 Month%' AND r.occurred_on IS NOT NULL"
    ).fetchall()
    td = today()
    out = []
    for r in rows:
        due = add_months(r["occurred_on"], 6)
        out.append({"tag": r["tag"], "base_108mo": r["occurred_on"], "due": due,
                    "overdue": due < td})
    return sorted(out, key=lambda x: x["due"])


def load_pendings(conn, asset_id):
    rows = conn.execute(
        "SELECT p.*, u.display_name AS author_name, cu.display_name AS completed_by_name "
        "FROM pending_entries p JOIN users u ON u.id = p.author_id "
        "LEFT JOIN users cu ON cu.id = p.completed_by "
        "WHERE p.asset_id = ? "
        "ORDER BY CASE p.status WHEN 'SUBMITTED' THEN 0 WHEN 'REVIEWED' THEN 1 ELSE 2 END, "
        "p.created_at DESC",
        (asset_id,),
    ).fetchall()
    out = []
    for r in rows:
        photos = conn.execute(
            "SELECT id, filename, caption, kind FROM pending_photos "
            "WHERE pending_entry_id = ? ORDER BY id", (r["id"],),
        ).fetchall()
        parts = conn.execute(
            "SELECT part_number, quantity FROM pending_parts WHERE pending_entry_id = ? ORDER BY id",
            (r["id"],),
        ).fetchall()
        d = dict(r)
        d["photos"] = [{"id": p["id"], "url": "/uploads/" + p["filename"],
                        "caption": p["caption"], "kind": p["kind"]} for p in photos]
        d["parts"] = [dict(p) for p in parts]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main():
    # A hosted process (Render/Railway/Fly/…) sets $PORT and needs 0.0.0.0.
    env_port = os.environ.get("PORT")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8000)
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if env_port else "127.0.0.1"))
    ap.add_argument("--reset", action="store_true", help="wipe data and reseed")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    if args.reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        fresh = True
        print("  !! --reset: wiped data/app.db (all app-entered pendings and edits gone)")
    seed()

    conn = get_db()
    n_pend = conn.execute("SELECT COUNT(*) c FROM pending_entries").fetchone()["c"]
    n_app = conn.execute("SELECT COUNT(*) c FROM pending_entries WHERE wo_code IS NULL").fetchone()["c"]
    n_edits = conn.execute("SELECT COUNT(*) c FROM record_changes").fetchone()["c"]
    conn.close()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port)
    print("\n  Site Portal")
    print("  " + "-" * 40)
    print("  running at   %s" % url)
    print("  logins       from source/Credentials.csv (synced each start)")
    print("  break-glass  admin / %s" % (os.environ.get("ADMIN_PASSWORD") or "admin123"))
    print("  database     %s  (%s)" % (DB_PATH, "freshly seeded" if fresh else "existing, kept"))
    print("  contents     %d pending entries (%d added in-app), %d logged record edits"
          % (n_pend, n_app, n_edits))
    print("  note         app-entered data lives in data/ — plain `python3 app.py` keeps it;")
    print("               `--reset` deletes it. Nothing else wipes the DB.")
    print("  stop         Ctrl-C\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
