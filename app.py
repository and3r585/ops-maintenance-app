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

try:
    from PIL import Image, ImageOps
    _HAVE_PIL = True
except ImportError:                       # app still runs; photos are stored as-is
    _HAVE_PIL = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
# $DATA_DIR lets a host point the SQLite DB + uploads at a persistent disk.
DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.path.join(DATA_DIR, "app.db")

# --- pending-entry photos ---------------------------------------------------
# The store is a plain directory today; every reference goes through photo_url()
# / photo_thumb_url() so it can move to object storage (e.g. Cloudflare R2) later
# by editing just those two functions and the upload write path.
PHOTO_MAX_EDGE = 4000      # longest edge kept on the full image (px)
PHOTO_THUMB_EDGE = 480     # longest edge of the list thumbnail (px)
PHOTO_JPEG_Q = 90          # full-image re-encode quality (visually lossless)
PHOTO_THUMB_Q = 80
PHOTO_RAW_CAP = 25 * 1024 * 1024   # byte ceiling for the decode / raw-fallback


def photo_url(filename):
    return "/uploads/" + filename


def thumb_name(filename):
    return os.path.splitext(filename)[0] + "_thumb.jpg"


def photo_thumb_url(filename):
    t = thumb_name(filename)
    return "/uploads/" + (t if os.path.isfile(os.path.join(UPLOAD_DIR, t)) else filename)


def _encode_image(im, fmt, quality):
    buf = io.BytesIO()
    if fmt == "PNG":
        im.save(buf, "PNG", optimize=True)
    else:
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def process_image(content, ext):
    """(main_bytes, main_ext, thumb_bytes|None). Downscales past PHOTO_MAX_EDGE and
    re-encodes; small images pass through untouched. Falls back to the raw bytes if
    the file can't be decoded (e.g. HEIC without a plugin)."""
    raw = content[:PHOTO_RAW_CAP]
    if not _HAVE_PIL:
        return raw, ext, None
    try:
        im = ImageOps.exif_transpose(Image.open(io.BytesIO(content)))
    except Exception:
        return raw, ext, None
    keep_png = ext == ".png" and im.mode in ("RGBA", "LA", "P")
    fmt, out_ext = ("PNG", ".png") if keep_png else ("JPEG", ".jpg")

    resized = max(im.size) > PHOTO_MAX_EDGE
    if not resized and len(content) <= 2 * 1024 * 1024:
        main_bytes, out_ext = raw, ext          # already small — no generation loss
    else:
        main = im.copy()
        if resized:
            main.thumbnail((PHOTO_MAX_EDGE, PHOTO_MAX_EDGE), Image.LANCZOS)
        main_bytes = _encode_image(main, fmt, PHOTO_JPEG_Q)

    thumb = im.copy()
    thumb.thumbnail((PHOTO_THUMB_EDGE, PHOTO_THUMB_EDGE), Image.LANCZOS)
    thumb_bytes = _encode_image(thumb, "JPEG", PHOTO_THUMB_Q)
    return main_bytes, out_ext, thumb_bytes


def backfill_thumbs():
    """Generate any missing list thumbnails for photos already on disk (one-off, cheap)."""
    if not _HAVE_PIL or not os.path.isdir(UPLOAD_DIR):
        return
    conn = get_db()
    made = 0
    for r in conn.execute("SELECT filename FROM pending_photos"):
        src = os.path.join(UPLOAD_DIR, r["filename"])
        dst = os.path.join(UPLOAD_DIR, thumb_name(r["filename"]))
        if not os.path.isfile(src) or os.path.isfile(dst):
            continue
        try:
            im = ImageOps.exif_transpose(Image.open(src))
            im.thumbnail((PHOTO_THUMB_EDGE, PHOTO_THUMB_EDGE), Image.LANCZOS)
            open(dst, "wb").write(_encode_image(im, "JPEG", PHOTO_THUMB_Q))
            made += 1
        except Exception:
            pass
    conn.close()
    if made:
        print("  thumbnails   generated %d missing" % made)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('ADMIN','TECHNICIAN','VIEW')),
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

-- Daily team plan: 10 team rows per date, each an (optional) pending entry + its technicians.
CREATE TABLE IF NOT EXISTS plan_team (
    plan_date  TEXT NOT NULL,
    team_no    INTEGER NOT NULL,
    pending_id INTEGER REFERENCES pending_entries(id),
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

    # --- migration: retire the early hard-coded `jobs` demo table.
    #     The planning board now schedules real pending entries, so plan_team
    #     references pending_entries instead of jobs. plan_team only holds
    #     day-scoped scratch assignments, so it is safe to rebuild.
    pt_cols = [r["name"] for r in conn.execute("PRAGMA table_info(plan_team)")]
    if pt_cols and "pending_id" not in pt_cols:
        conn.executescript(
            "DROP TABLE IF EXISTS plan_team;"
            "CREATE TABLE plan_team ("
            "  plan_date  TEXT NOT NULL,"
            "  team_no    INTEGER NOT NULL,"
            "  pending_id INTEGER REFERENCES pending_entries(id),"
            "  PRIMARY KEY (plan_date, team_no));"
        )
    conn.executescript("DROP TABLE IF EXISTS job_activity; DROP TABLE IF EXISTS jobs;")

    # --- migration: widen the users.role CHECK to allow the read-only 'VIEW' role ---
    udef = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if udef and "'VIEW'" not in udef["sql"]:
        conn.executescript(
            "PRAGMA foreign_keys=OFF;"
            "ALTER TABLE users RENAME TO users_old;"
            "CREATE TABLE users ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  username TEXT UNIQUE NOT NULL,"
            "  password_hash TEXT NOT NULL,"
            "  salt TEXT NOT NULL,"
            "  role TEXT NOT NULL CHECK (role IN ('ADMIN','TECHNICIAN','VIEW')),"
            "  display_name TEXT NOT NULL,"
            "  active INTEGER NOT NULL DEFAULT 1,"
            "  created_at TEXT NOT NULL);"
            "INSERT INTO users SELECT id,username,password_hash,salt,role,display_name,active,created_at FROM users_old;"
            "DROP TABLE users_old;"
            "PRAGMA foreign_keys=ON;"
        )

    # --- users: synced from source/Credentials.csv on every start (+ `admin` break-glass) ---
    sync_users(conn, seed_data.load_credentials())

    # --- one-time turbine / asset / history import.
    #     Runs ONLY when the database has never been populated. Once assets exist the
    #     database is the sole source of truth and source/_archived/*.csv is never read.
    if conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"] == 0:
        _first_time_import(conn)

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


def _first_time_import(conn):
    """Populate an empty database from the archived one-time-import CSVs."""
    data = seed_data.load_data()

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
    print("  imported     turbines, services, history and pendings from source/_archived/")


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
        """role: a single role string or an iterable of allowed roles."""
        user = self._current_user(conn)
        if not user:
            raise ApiError(401, "Not authenticated")
        if role is not None:
            allowed = (role,) if isinstance(role, str) else tuple(role)
            if user["role"] not in allowed:
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
            elevated = user["role"] in ("ADMIN", "VIEW")   # VIEW sees everything an admin sees
            visible = [dict(r) for r in rows if r["min_role"] == "TECHNICIAN" or elevated]
            return 200, {"modules": visible}

        # --- site dashboard (any signed-in user; read-only figures) ---
        if parts == ["dashboard"] and method == "GET":
            self._require(conn)
            by_status = {r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) c FROM pending_entries GROUP BY status")}
            open_pendings = sum(c for s, c in by_status.items() if s != "COMPLETED")

            services = service_worklist(conn)
            retro_rows = conn.execute(
                "SELECT r.id, a.id AS asset_id, a.tag, r.name, r.status "
                "FROM asset_records r JOIN assets a ON a.id = r.asset_id "
                "WHERE r.category = 'retrofit' AND r.status IN ('outstanding','in_progress') "
                "ORDER BY r.name, a.tag"
            ).fetchall()
            retro_by_name = {}
            for r in retro_rows:
                g = retro_by_name.setdefault(r["name"], {"name": r["name"], "items": []})
                g["items"].append({"asset_id": r["asset_id"], "tag": r["tag"],
                                   "record_id": r["id"], "status": r["status"]})
            incomplete_retrofits = sorted(retro_by_name.values(), key=lambda g: -len(g["items"]))

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
                        {"url": photo_url(ph["filename"]),
                         "thumb": photo_thumb_url(ph["filename"]), "kind": ph["kind"]})
            counts = {s: 0 for s in ("SUBMITTED", "REVIEWED", "COMPLETED")}
            for r in conn.execute("SELECT status, COUNT(*) c FROM pending_entries GROUP BY status"):
                counts[r["status"]] = r["c"]
            return 200, {"pendings": rows, "counts": counts}

        # --- daily team plan ---
        if parts == ["plan"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
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
                "INSERT OR IGNORE INTO plan_team (plan_date, team_no, pending_id) VALUES (?,?,NULL)",
                (date, team_no),
            )
            if op == "set_job":
                pending_id = data.get("job_id")
                task = conn.execute(
                    "SELECT id, status FROM pending_entries WHERE id = ?", (pending_id,)
                ).fetchone()
                if not task:
                    raise ApiError(404, "Pending entry not found")
                if task["status"] == "COMPLETED":
                    raise ApiError(409, "That pending entry is already completed")
                # a pending sits on at most one team per date
                conn.execute("UPDATE plan_team SET pending_id = NULL WHERE plan_date = ? AND pending_id = ?",
                             (date, pending_id))
                conn.execute("UPDATE plan_team SET pending_id = ? WHERE plan_date = ? AND team_no = ?",
                             (pending_id, date, team_no))
            elif op == "clear_job":
                conn.execute("UPDATE plan_team SET pending_id = NULL WHERE plan_date = ? AND team_no = ?",
                             (date, team_no))
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
            ordered = [r["id"] for r in conn.execute("SELECT id FROM assets ORDER BY tag")]
            tag_by_id = {r["id"]: r["tag"] for r in conn.execute("SELECT id, tag FROM assets")}
            idx = ordered.index(asset["id"])
            prev_id = ordered[idx - 1] if idx > 0 else ordered[-1]
            next_id = ordered[(idx + 1) % len(ordered)]
            return 200, {
                "asset": dict(asset),
                "prev": {"id": prev_id, "tag": tag_by_id[prev_id]},
                "next": {"id": next_id, "tag": tag_by_id[next_id]},
                "services": services,
                "hv": [dict(r) for r in recs if r["category"] == "hv"],
                "stat": [dict(r) for r in recs if r["category"] == "stat"],
                "retrofits": [dict(r) for r in recs if r["category"] == "retrofit"],
                "components": [dict(r) for r in recs if r["category"] == "component"],
                "blades": [dict(r) for r in recs if r["category"] == "blade"],
                "history": [dict(r) for r in history],
                "next_service": next_service(services, asset["install_date"]),
            }

        # admin edits the free-text defect / operational-issue note on an asset
        if len(parts) == 2 and parts[0] == "assets" and method == "PATCH":
            user = self._require(conn, "ADMIN")
            asset = conn.execute("SELECT id, tag, defect FROM assets WHERE id = ?",
                                 (parts[1],)).fetchone()
            if not asset:
                raise ApiError(404, "Asset not found")
            data = self._json_body()
            if "defect" not in data:
                raise ApiError(400, "Nothing to update")
            new = (data.get("defect") or "").strip() or None
            old = asset["defect"]
            if (old or None) != (new or None):
                conn.execute("UPDATE assets SET defect = ? WHERE id = ?", (new, asset["id"]))
                conn.execute(
                    "INSERT INTO record_changes "
                    "(record_id, asset_id, category, record_name, field, old_value, new_value, "
                    "changed_by, changed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (asset["id"], asset["id"], "defect", "Defect / operational issue",
                     "detail", old, new, user["id"], now()))
            return 200, {"ok": True, "defect": new}

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
                user = self._require(conn, ("ADMIN", "TECHNICIAN"))
                return self._create_pending(conn, asset_id, user)

        # --- pendings ---
        if parts == ["pendings", "export"] and method == "GET":
            self._require(conn)
            want = (query.get("status", [""])[0] or "").upper()
            sql = (
                "SELECT a.tag AS turbine, p.priority, p.system, p.status, "
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
            w.writerow(["Turbine", "Priority", "System", "Status", "Logged by",
                        "Date", "Note", "Parts SO", "Parts", "Completed by",
                        "Completed", "Completion note", "Photos"])
            for r in rows:
                w.writerow([
                    r["turbine"],
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
            row = conn.execute("SELECT status, priority FROM pending_entries WHERE id = ?",
                               (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "Pending entry not found")
            data = self._json_body()
            status = (data.get("status") or "").upper()
            if status not in ("SUBMITTED", "REVIEWED"):
                raise ApiError(400, "Admin can set Submitted or Reviewed. "
                                    "Completion is done by a technician with evidence.")
            if status == "REVIEWED":
                # a priority of 1-5 must be set before an entry can be reviewed
                pr = data.get("priority", row["priority"])
                try:
                    pr = int(pr)
                except (TypeError, ValueError):
                    pr = None
                if pr is None or not (1 <= pr <= 5):
                    raise ApiError(400, "Assign a priority of 1-5 before marking this entry Reviewed.")
                conn.execute("UPDATE pending_entries SET status = ?, priority = ? WHERE id = ?",
                             (status, pr, parts[1]))
            else:  # leaving Reviewed drops any parts reservation
                conn.execute("UPDATE pending_entries SET status = ? WHERE id = ?", (status, parts[1]))
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
            user = self._require(conn, ("ADMIN", "TECHNICIAN"))
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

        # --- data explorer (ADMIN edits; VIEW can read/export the same tables) ---
        if parts == ["explorer", "categories"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            return 200, {"categories": [
                {"key": k, "label": lbl, "value_field": vf}
                for k, lbl, vf in EXPLORER_CATEGORIES]}

        if parts == ["explorer", "matrix"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
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
            self._require(conn, ("ADMIN", "VIEW"))
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

        # completions report: every asset_record completed in the window, one sheet
        # per asset tab, regardless of whether the date was imported or set in the app.
        if parts == ["explorer", "changes"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            frm = (query.get("from", [""])[0] or "").strip()
            to = (query.get("to", [""])[0] or "").strip()
            for d in (frm, to):
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    raise ApiError(400, "from and to dates are required (YYYY-MM-DD)")
            if frm > to:
                frm, to = to, frm

            sheets = []
            total = 0
            for key, label, _vf in EXPLORER_CATEGORIES:
                rows = conn.execute(
                    "SELECT a.tag, r.name, r.occurred_on, r.status FROM asset_records r "
                    "JOIN assets a ON a.id = r.asset_id "
                    "WHERE r.category = ? AND r.occurred_on IS NOT NULL "
                    "AND r.occurred_on BETWEEN ? AND ? "
                    "ORDER BY r.occurred_on, a.tag, r.name", (key, frm, to)).fetchall()
                head = ["Turbine", "Record", "Completed"] + (["Status"] if key == "retrofit" else [])
                data = [head]
                for r in rows:
                    line = [r["tag"], r["name"], r["occurred_on"]]
                    if key == "retrofit":
                        line.append(r["status"] or "")
                    data.append(line)
                total += len(rows)
                sheets.append((label, data))          # one tab per type, even if empty

            prows = conn.execute(
                "SELECT a.tag, p.priority, p.status, p.parts_reserved_at, p.completed_at, "
                "cu.display_name AS completed_by_name, p.note "
                "FROM pending_entries p JOIN assets a ON a.id = p.asset_id "
                "LEFT JOIN users cu ON cu.id = p.completed_by "
                "WHERE substr(COALESCE(p.completed_at,''),1,10) BETWEEN ? AND ? "
                "   OR substr(COALESCE(p.parts_reserved_at,''),1,10) BETWEEN ? AND ? "
                "ORDER BY p.completed_at, a.tag", (frm, to, frm, to)).fetchall()
            pdata = [["Turbine", "Priority", "Status", "Parts reserved", "Completed",
                      "Completed by", "Note"]]
            for r in prows:
                pdata.append([
                    r["tag"], r["priority"] if r["priority"] is not None else "", r["status"],
                    (r["parts_reserved_at"] or "")[:10], (r["completed_at"] or "")[:10],
                    r["completed_by_name"] or "",
                    (r["note"] or "").replace("\r\n", " ").replace("\n", " "),
                ])
            total += len(prows)
            sheets.append(("Pendings", pdata))

            if total == 0:
                raise ApiError(404, "No completions recorded between %s and %s" % (frm, to))
            fname = "completions-%s_to_%s.xlsx" % (frm, to)
            return 200, RawResponse(
                build_xlsx(sheets),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                fname)

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
            main_bytes, out_ext, thumb_bytes = process_image(f["content"], ext)
            fname = "p%d_%s%s" % (entry_id, secrets.token_hex(6), out_ext)
            with open(os.path.join(UPLOAD_DIR, fname), "wb") as out:
                out.write(main_bytes)
            if thumb_bytes:
                with open(os.path.join(UPLOAD_DIR, thumb_name(fname)), "wb") as out:
                    out.write(thumb_bytes)
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
        # Upload filenames are content-unique and never rewritten -> cache hard.
        lastmod = time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                time.gmtime(os.stat(full).st_mtime))
        cache = "public, max-age=31536000, immutable"
        if self.headers.get("If-Modified-Since") == lastmod:
            self.send_response(304)
            self.send_header("Cache-Control", cache)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.send_header("Last-Modified", lastmod)
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if full.startswith(WEB_DIR) and os.path.isfile(full):
            self._send_file(full)
            return
        # The SPA uses hash routing, so genuine client routes never reach here.
        # Fall back to index.html only for extensionless paths; scanner probes like
        # /.env, /wp-login.php or /db.sql get a real 404 instead of the app shell.
        if "." in os.path.basename(path):
            raise ApiError(404, "Not found")
        self._send_file(os.path.join(WEB_DIR, "index.html"))

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

    def task(row):
        note = (row["note"] or "").strip().replace("\r\n", "\n")
        title = note.split("\n", 1)[0]
        if len(title) > 90:
            title = title[:88].rstrip() + "…"
        return {"id": row["id"], "title": title or "(no description)",
                "asset_tag": row["asset_tag"], "priority": row["priority"],
                "status": row["status"]}

    placed = conn.execute(
        "SELECT t.team_no, p.id, p.note, p.priority, p.status, a.tag AS asset_tag "
        "FROM plan_team t JOIN pending_entries p ON p.id = t.pending_id "
        "JOIN assets a ON a.id = p.asset_id WHERE t.plan_date = ?",
        (date,),
    ).fetchall()
    task_by_team = {p["team_no"]: task(p) for p in placed}
    placed_ids = {p["id"] for p in placed}

    teams = []
    for n in range(1, 11):
        teams.append({
            "team_no": n,
            "job": task_by_team.get(n),
            "members": by_team.get(n, []),
        })

    # backlog = open pending entries an admin has reviewed (triaged, ready to schedule),
    # highest priority first, capped so the rail stays usable.
    rows = conn.execute(
        "SELECT p.id, p.note, p.priority, p.status, a.tag AS asset_tag "
        "FROM pending_entries p JOIN assets a ON a.id = p.asset_id "
        "WHERE p.status = 'REVIEWED' "
        "ORDER BY p.priority IS NULL, p.priority, p.created_at LIMIT 60"
    ).fetchall()
    backlog = [task(r) for r in rows if r["id"] not in placed_ids]
    open_reviewed = conn.execute(
        "SELECT COUNT(*) c FROM pending_entries WHERE status = 'REVIEWED'"
    ).fetchone()["c"]

    assigned_ids = {m["id"] for m in members}
    return {
        "date": date,
        "teams": teams,
        "backlog": backlog,
        "backlog_total": open_reviewed,
        "available": [t for t in techs if t["available"] and t["id"] not in assigned_ids],
        "unavailable": [t for t in techs if not t["available"]],
        "assigned_count": len(assigned_ids),
    }


def _service_month(name):
    m = re.match(r"^\s*(\d+)\s*month", name or "", re.I)
    return int(m.group(1)) if m else None


def next_service(svc_records, install_date):
    """The first incomplete service after the last completed one.
    svc_records: dicts with 'name' and a 'date' (or 'occurred_on'). Planned date =
    last completion + the interval, or install date + months if nothing is done yet.
    Returns {record_id, name, planned, overdue} or None when every service is complete."""
    seq = []
    for s in svc_records:
        m = _service_month(s["name"])
        if m is not None:                   # the 5-year oil exchange is not in the sequence
            seq.append({"id": s.get("id"), "name": s["name"], "month": m,
                        "date": s.get("date") or s.get("occurred_on")})
    seq.sort(key=lambda s: s["month"])
    completed = [s for s in seq if s["date"]]
    last_m = completed[-1]["month"] if completed else 0
    last_date = completed[-1]["date"] if completed else None
    nxt = next((s for s in seq if s["month"] > last_m), None)
    if not nxt:
        return None
    interval = nxt["month"] - last_m if last_m else nxt["month"]
    base = last_date or install_date
    planned = add_months(base, interval) if base else None
    return {"record_id": nxt["id"], "name": nxt["name"], "planned": planned,
            "overdue": bool(planned and planned < today())}


def service_worklist(conn):
    """One row per turbine: its next incomplete service, ready to complete from the
    dashboard drill-down."""
    rows = conn.execute(
        "SELECT r.id, r.name, r.occurred_on, a.id AS asset_id, a.tag, a.install_date "
        "FROM asset_records r JOIN assets a ON a.id = r.asset_id WHERE r.category = 'service'"
    ).fetchall()
    by_asset = {}
    for r in rows:
        g = by_asset.setdefault(r["asset_id"],
                                {"tag": r["tag"], "install": r["install_date"], "svc": []})
        g["svc"].append({"id": r["id"], "name": r["name"], "occurred_on": r["occurred_on"]})
    out = []
    for aid, info in by_asset.items():
        n = next_service(info["svc"], info["install"])
        if n:
            out.append({"asset_id": aid, "tag": info["tag"], **n})
    out.sort(key=lambda x: (x["planned"] is None, x["planned"] or "", x["tag"]))
    return out


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
        d["photos"] = [{"id": p["id"], "url": photo_url(p["filename"]),
                        "thumb": photo_thumb_url(p["filename"]),
                        "caption": p["caption"], "kind": p["kind"]} for p in photos]
        d["parts"] = [dict(p) for p in parts]
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def _guard_reset(force):
    """Refuse --reset if the database holds data that was entered through the app
    (photos, record edits, day plans, technician-logged pendings) unless --force."""
    try:
        c = get_db()
        n = sum(c.execute("SELECT COUNT(*) c FROM " + t).fetchone()["c"]
                for t in ("pending_photos", "record_changes", "plan_team"))
        n += c.execute("SELECT COUNT(*) c FROM pending_entries WHERE wo_code IS NULL").fetchone()["c"]
        c.close()
    except sqlite3.Error:
        n = -1                                   # can't tell -> treat as risky
    if n != 0 and not force:
        detail = "an unreadable schema" if n < 0 else "%d app-entered item(s)" % n
        sys.exit(
            "\n  --reset refused: data/app.db contains %s\n"
            "  (photos / record edits / day plans / logged pendings).\n"
            "  This deletes them permanently. Re-run with  --reset --force  only if\n"
            "  you are certain, or back up data/app.db first.\n" % detail)


def main():
    # A hosted process (Render/Railway/Fly/…) sets $PORT and needs 0.0.0.0.
    env_port = os.environ.get("PORT")
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(env_port) if env_port else 8000)
    ap.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if env_port else "127.0.0.1"))
    ap.add_argument("--reset", action="store_true",
                    help="DESTRUCTIVE: delete data/app.db and re-import from source/_archived/*.csv")
    ap.add_argument("--force", action="store_true",
                    help="allow --reset even when the database holds app-entered data")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    fresh = not os.path.exists(DB_PATH)
    if args.reset and os.path.exists(DB_PATH):
        _guard_reset(args.force)
        os.remove(DB_PATH)
        fresh = True
        print("  !! --reset: wiped data/app.db")
    seed()
    backfill_thumbs()

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
    print("  photos       %s" % ("Pillow ready — uploads resized to %dpx, thumbnails on"
                                 % PHOTO_MAX_EDGE if _HAVE_PIL
                                 else "Pillow NOT installed — photos stored as-is (pip install pillow)"))
    print("  note         app-entered data lives in data/ — plain `python3 app.py` keeps it;")
    print("               `--reset` deletes it. Nothing else wipes the DB.")
    print("  stop         Ctrl-C\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
