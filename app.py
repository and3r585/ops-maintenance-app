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
import datetime
import hashlib
import io
import json
import mimetypes
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from email.message import EmailMessage
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
    role          TEXT NOT NULL CHECK (role IN ('ADMIN','TECHNICIAN','VIEW','CONTRACTOR')),
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

-- Technician roster. roster_tech is the master technician list (also drives the
-- Notification Request rail); roster_day holds one code per technician per day
-- (blank/available = no row). Seeded once from the Manplan tab, then app-owned.
CREATE TABLE IF NOT EXISTS roster_tech (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    user_id       INTEGER REFERENCES users(id),   -- optional link to a login account
    active        INTEGER NOT NULL DEFAULT 1,      -- 0 = archived (calendar kept)
    is_contractor INTEGER NOT NULL DEFAULT 0,      -- 1 = contractor: not on the roster grid,
                                                   --     always available in Notification Request
    sort          INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    archived_at   TEXT
);
CREATE TABLE IF NOT EXISTS roster_day (
    tech_id INTEGER NOT NULL REFERENCES roster_tech(id),
    on_date TEXT NOT NULL,                       -- ISO date
    code    TEXT NOT NULL,                       -- a Manplan Key code
    PRIMARY KEY (tech_id, on_date)
);
CREATE INDEX IF NOT EXISTS ix_roster_day_date ON roster_day(on_date);

-- Free-text note against a roster day (team calendar). One per date.
CREATE TABLE IF NOT EXISTS roster_note (
    on_date    TEXT PRIMARY KEY,                 -- ISO date
    note       TEXT NOT NULL,
    author_id  INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Note against one technician's day (surfaced on TRG / training days).
CREATE TABLE IF NOT EXISTS roster_train_note (
    tech_id    INTEGER NOT NULL REFERENCES roster_tech(id),
    on_date    TEXT NOT NULL,
    note       TEXT NOT NULL,
    author_id  INTEGER REFERENCES users(id),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tech_id, on_date)
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
    starts_on   TEXT,                   -- planned start date for an upcoming service (cleared once completed)
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

-- Notification Request: one row per team on a roster date. A team becomes a
-- notification (turbine + contract type + description + optional ATS case) worked
-- by up to 4 technicians. On submit it is written into that asset's history.
CREATE TABLE IF NOT EXISTS notif_request (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_date     TEXT NOT NULL,
    team_no       INTEGER NOT NULL,
    asset_id      INTEGER REFERENCES assets(id),
    contract_type TEXT,
    description   TEXT,
    ats_case      TEXT,
    son           TEXT,                 -- optional Service Order Number
    submitted_at  TEXT,
    submitted_by  INTEGER REFERENCES users(id),
    history_id    INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE (plan_date, team_no)
);
CREATE TABLE IF NOT EXISTS notif_member (
    plan_date TEXT NOT NULL,
    team_no   INTEGER NOT NULL,
    tech_id   INTEGER NOT NULL REFERENCES roster_tech(id),
    slot      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (plan_date, team_no, tech_id)
);

-- Notification Request outbox: on submit, one row per complete request is queued
-- here so it can be delivered to the shared company workbook independently of the
-- submit itself (decoupled — submit never blocks on the external system). A
-- background worker delivers PENDING rows via webhook or email when configured
-- (see NOTIF_OUTBOX_* below); until then they simply wait and an admin can mark
-- them done by hand.
CREATE TABLE IF NOT EXISTS notif_outbox (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id TEXT NOT NULL,          -- one per Submit action (idempotency key)
    plan_date     TEXT NOT NULL,
    team_no       INTEGER NOT NULL,
    contract_type TEXT,
    turbine       TEXT,                   -- "B18 Kilgallioch" or "" (non-turbine)
    payload       TEXT NOT NULL,          -- JSON: named fields for a row-append action
    cells         TEXT NOT NULL,          -- JSON: the 18 sheet cells, column order
    status        TEXT NOT NULL DEFAULT 'PENDING'
                    CHECK (status IN ('PENDING','SENT','FAILED','SKIPPED')),
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    created_at    TEXT NOT NULL,
    created_by    INTEGER REFERENCES users(id),
    sent_at       TEXT,
    sent_via      TEXT,                   -- 'webhook' | 'manual'
    UNIQUE (submission_id, team_no)
);
CREATE INDEX IF NOT EXISTS ix_notif_outbox_status ON notif_outbox(status);

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
    -- last add-on (part / photo) made while REVIEWED
    updated_by     INTEGER REFERENCES users(id),
    updated_at     TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_parts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_entry_id  INTEGER NOT NULL REFERENCES pending_entries(id),
    part_number       TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    added_by          INTEGER REFERENCES users(id),
    added_at          TEXT
);

CREATE TABLE IF NOT EXISTS pending_photos (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pending_entry_id  INTEGER NOT NULL REFERENCES pending_entries(id),
    filename          TEXT NOT NULL,
    caption           TEXT,
    kind              TEXT NOT NULL DEFAULT 'note',   -- 'note' | 'reviewed' | 'evidence'
    added_by          INTEGER REFERENCES users(id),
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

-- small key/value store for one-shot data migrations and app-wide flags
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
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

    # --- repair: on some SQLite builds, rebuilding the `users` table via
    #     RENAME/CREATE/DROP rewrites "REFERENCES users" to "REFERENCES users_old" in
    #     every referencing table, then users_old is dropped — leaving dangling foreign
    #     keys that break the next INSERT. Rebuild each affected table with the
    #     reference pointed back at `users`. ---
    bad = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%users_old%'")]
    if bad:
        for name in bad:
            old_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name = ?", (name,)
            ).fetchone()["sql"]
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % name)]
            fixed = old_sql.replace("users_old", "users")
            tmp = name + "__fkfix"
            fixed = re.sub(r'CREATE TABLE\s+["\']?' + re.escape(name) + r'["\']?',
                           'CREATE TABLE "%s"' % tmp, fixed, count=1)
            collist = ",".join('"%s"' % c for c in cols)
            conn.executescript(
                "PRAGMA legacy_alter_table=ON;"
                "PRAGMA foreign_keys=OFF;"
                + fixed + ";"
                + 'INSERT INTO "%s" (%s) SELECT %s FROM "%s";' % (tmp, collist, collist, name)
                + 'DROP TABLE "%s";' % name
                + 'ALTER TABLE "%s" RENAME TO "%s";' % (tmp, name)
                + "PRAGMA foreign_keys=ON;"
                + "PRAGMA legacy_alter_table=OFF;")
        conn.commit()
        conn.close()
        conn = get_db()                      # reopen so the patched schema is reloaded
        print("  repaired     users foreign key on %d table(s): %s" % (len(bad), ", ".join(bad)))

    # --- migration: retire early demo/scratch tables. The day-plan board (jobs,
    #     plan_team, plan_member) is replaced by Notification Request; all three
    #     only ever held transient scratch data. ---
    conn.executescript(
        "DROP TABLE IF EXISTS job_activity; DROP TABLE IF EXISTS jobs;"
        "DROP TABLE IF EXISTS plan_team; DROP TABLE IF EXISTS plan_member;")

    # --- migration: mark where an asset_history row came from ('import' | 'notification') ---
    if "source" not in [r["name"] for r in conn.execute("PRAGMA table_info(asset_history)")]:
        conn.execute("ALTER TABLE asset_history ADD COLUMN source TEXT")

    # --- migration: additive columns (idempotent) ---
    def _addcol(table, col, decl):
        if col not in [r["name"] for r in conn.execute("PRAGMA table_info(%s)" % table)]:
            conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
    _addcol("asset_records", "starts_on", "TEXT")
    _addcol("notif_request", "son", "TEXT")
    _addcol("pending_parts", "added_by", "INTEGER")
    _addcol("pending_parts", "added_at", "TEXT")
    _addcol("pending_photos", "added_by", "INTEGER")
    _addcol("pending_entries", "updated_by", "INTEGER")
    _addcol("pending_entries", "updated_at", "TEXT")
    _addcol("roster_tech", "is_contractor", "INTEGER NOT NULL DEFAULT 0")

    # --- migration: widen the users.role CHECK (adds 'VIEW', then 'CONTRACTOR') ---
    udef = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if udef and "'CONTRACTOR'" not in udef["sql"]:
        conn.executescript(
            "PRAGMA legacy_alter_table=ON;"       # don't rewrite FK refs in other tables
            "PRAGMA foreign_keys=OFF;"
            "ALTER TABLE users RENAME TO users_old;"
            "CREATE TABLE users ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  username TEXT UNIQUE NOT NULL,"
            "  password_hash TEXT NOT NULL,"
            "  salt TEXT NOT NULL,"
            "  role TEXT NOT NULL CHECK (role IN ('ADMIN','TECHNICIAN','VIEW','CONTRACTOR')),"
            "  display_name TEXT NOT NULL,"
            "  active INTEGER NOT NULL DEFAULT 1,"
            "  created_at TEXT NOT NULL);"
            "INSERT INTO users SELECT id,username,password_hash,salt,role,display_name,active,created_at FROM users_old;"
            "DROP TABLE users_old;"
            "PRAGMA foreign_keys=ON;"
            "PRAGMA legacy_alter_table=OFF;"
        )

    # --- users: synced from source/Credentials.csv on every start (+ `admin` break-glass) ---
    sync_users(conn, seed_data.load_credentials())

    # --- one-shot: replace every service record from the comprehensive service-date
    #     export (KGH Service dates_Comprehensive). Runs once against an existing
    #     database; on a brand-new database _first_time_import loads the same data, so
    #     this only sets the flag. Service completions are editable in the app, so this
    #     never re-runs — later corrections are made in the app, not the file. ---
    if not _meta_get(conn, "service_dates_comprehensive"):
        if conn.execute(
            "SELECT COUNT(*) c FROM asset_records WHERE category = 'service'"
        ).fetchone()["c"]:
            n = _replace_service_records(conn, seed_data.load_service_dates())
            print("  replaced     %d service records from the comprehensive export" % n)
        _meta_set(conn, "service_dates_comprehensive", "1")

    # --- one-shot: build the technician roster (roster_tech + roster_day) from the
    #     Manplan grid. After this the roster tables are the live record and the
    #     master technician list for Notification Request. The old `roster` table and
    #     the user_id-keyed notif_member are retired here. ---
    if not _meta_get(conn, "roster_calendar_v1"):
        if "tech_id" not in [r["name"] for r in conn.execute("PRAGMA table_info(notif_member)")]:
            conn.executescript(
                "DROP TABLE IF EXISTS notif_member;"
                "CREATE TABLE notif_member ("
                "  plan_date TEXT NOT NULL, team_no INTEGER NOT NULL,"
                "  tech_id INTEGER NOT NULL REFERENCES roster_tech(id),"
                "  slot INTEGER NOT NULL DEFAULT 0,"
                "  PRIMARY KEY (plan_date, team_no, tech_id));")
        nt, nd = _seed_roster(conn)
        if nt:
            print("  roster       %d technicians, %d day entries from the Manplan grid" % (nt, nd))
        conn.execute("DROP TABLE IF EXISTS roster")
        _meta_set(conn, "roster_calendar_v1", "1")

    # --- one-shot: a couple of named technicians that weren't TECHNICIAN-role logins
    #     (Scott Clydesdale, Stuart Cant) but are in the Manplan and may join a team. ---
    if not _meta_get(conn, "roster_extra_techs_v1"):
        added = _add_roster_techs(conn, ["Scott Clydesdale", "Stuart Cant"])
        if added:
            print("  roster       added %d extra technician(s) from the Manplan grid" % added)
        _meta_set(conn, "roster_extra_techs_v1", "1")

    # --- contractors: synced from the CONTRACTOR logins on every start. They get a
    #     roster_tech row (is_contractor=1) so they can be dragged onto a team, but they
    #     never appear on the roster calendar and are always available. ---
    sync_contractors(conn)

    # --- one-time turbine / asset / history import.
    #     Runs ONLY when the database has never been populated. Once assets exist the
    #     database is the sole source of truth and source/_archived/*.csv is never read.
    if conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"] == 0:
        _first_time_import(conn)

    # --- condition monitoring (SMP): replaced wholesale from source/KGH_SMP.csv every
    #     start (after the assets exist) ---
    sync_smp(conn, seed_data.load_smp())

    # navigation registry — re-synced every start so role changes take effect.
    #   technicians: Site Dashboard + Asset Information   admins: + Planning + Data Explorer
    for key, name, min_role, sort in [
        ("dashboard", "Site Dashboard", "TECHNICIAN", 5),
        ("assets", "Asset Information", "TECHNICIAN", 10),
        ("roster", "Technician Roster", "TECHNICIAN", 15),
        ("planning", "Notification Request", "ADMIN", 20),
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


def _meta_get(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def _meta_set(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def _replace_service_records(conn, svc_by_tag):
    """Discard all category='service' asset_records and re-insert from the
    comprehensive export. svc_by_tag: {tag: [{name, date, detail, sort}, ...]}."""
    aid = {r["tag"]: r["id"] for r in conn.execute("SELECT id, tag FROM assets")}
    conn.execute("DELETE FROM asset_records WHERE category = 'service'")
    n = 0
    for tag, recs in svc_by_tag.items():
        if tag not in aid:
            continue
        for r in recs:
            conn.execute(
                "INSERT INTO asset_records (asset_id,category,name,occurred_on,detail,sort) "
                "VALUES (?,?,?,?,?,?)",
                (aid[tag], "service", r["name"], r.get("date"), r.get("detail"),
                 r.get("sort", 0)),
            )
            n += 1
    return n


def _seed_roster(conn):
    """Create roster_tech from the active technician logins and backfill roster_day
    from the Manplan grid. Runs once — returns (technicians, day_entries)."""
    if conn.execute("SELECT COUNT(*) c FROM roster_tech").fetchone()["c"]:
        return 0, 0
    users = conn.execute(
        "SELECT id, display_name FROM users WHERE role = 'TECHNICIAN' AND active = 1 "
        "ORDER BY display_name"
    ).fetchall()
    for i, u in enumerate(users):
        conn.execute(
            "INSERT INTO roster_tech (name, user_id, active, sort, created_at) "
            "VALUES (?,?,1,?,?)", (u["display_name"], u["id"], i, now()))
    tid = {r["name"].strip().lower(): r["id"]
           for r in conn.execute("SELECT id, name FROM roster_tech")}
    days = 0
    for name, entries in seed_data.load_roster().items():
        t = tid.get(name.strip().lower())
        if not t:
            continue
        for iso, code in entries.items():
            conn.execute(
                "INSERT OR IGNORE INTO roster_day (tech_id, on_date, code) VALUES (?,?,?)",
                (t, iso, code))
            days += 1
    return len(users), days


def _add_roster_techs(conn, names):
    """Add named technicians to roster_tech (linked to a login if the display name
    matches) and backfill roster_day from the Manplan grid. Skips names already there."""
    grid = None
    added = 0
    for name in names:
        if conn.execute("SELECT 1 FROM roster_tech WHERE name = ?", (name,)).fetchone():
            continue
        u = conn.execute("SELECT id FROM users WHERE display_name = ?", (name,)).fetchone()
        mx = conn.execute("SELECT COALESCE(MAX(sort), 0) m FROM roster_tech").fetchone()["m"]
        cur = conn.execute(
            "INSERT INTO roster_tech (name, user_id, active, sort, created_at) VALUES (?,?,1,?,?)",
            (name, u["id"] if u else None, mx + 1, now()))
        if grid is None:
            grid = seed_data.load_roster()
        for iso, code in (grid.get(name) or {}).items():
            conn.execute("INSERT OR IGNORE INTO roster_day (tech_id, on_date, code) VALUES (?,?,?)",
                         (cur.lastrowid, iso, code))
        added += 1
    return added


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

    # (condition monitoring / SMP is synced separately, on every start — see sync_smp)

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

    # (technician roster: seeded by _seed_roster from seed(), not here)

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


def sync_contractors(conn):
    """Keep a roster_tech row (is_contractor=1) for every active CONTRACTOR login, so
    contractors can be dragged onto Notification Request teams. Runs on every start;
    a contractor whose login is gone is archived, not deleted."""
    users = {r["id"]: r for r in conn.execute(
        "SELECT id, display_name FROM users WHERE role = 'CONTRACTOR' AND active = 1")}
    have = {r["user_id"]: r for r in conn.execute(
        "SELECT * FROM roster_tech WHERE is_contractor = 1")}
    mx = conn.execute("SELECT COALESCE(MAX(sort), 0) m FROM roster_tech").fetchone()["m"]
    for uid, u in users.items():
        row = have.get(uid)
        if row is None:
            mx += 1
            conn.execute(
                "INSERT INTO roster_tech (name, user_id, active, is_contractor, sort, created_at) "
                "VALUES (?,?,1,1,?,?)", (u["display_name"], uid, mx, now()))
        elif not row["active"] or row["name"] != u["display_name"]:
            conn.execute("UPDATE roster_tech SET name = ?, active = 1 WHERE id = ?",
                         (u["display_name"], row["id"]))
    for uid, row in have.items():
        if uid not in users and row["active"]:
            conn.execute("UPDATE roster_tech SET active = 0, archived_at = ? WHERE id = ?",
                         (now(), row["id"]))


def sync_smp(conn, smp):
    """Replace every asset's condition-monitoring (SMP) state from source/KGH_SMP.csv.
    The old values are discarded wholesale each start; a turbine absent from the file
    is left blank."""
    conn.execute("UPDATE assets SET smp_data_date=NULL, smp_gearbox=NULL, "
                 "smp_generator=NULL, smp_main_bearing=NULL, smp_observations=NULL")
    applied = 0
    for tag, s in (smp or {}).items():
        cur = conn.execute(
            "UPDATE assets SET smp_data_date=?, smp_gearbox=?, smp_generator=?, "
            "smp_main_bearing=?, smp_observations=? WHERE tag=?",
            (s.get("data_date"), s.get("gearbox"), s.get("generator"),
             s.get("main_bearing"), s.get("observations"), tag))
        applied += cur.rowcount
    return applied


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

# read-only, non-turbine tables shown in the Data Explorer as a flat list
EXPLORER_FLAT = {"roster_note": "Roster notes", "service_start": "Service start dates"}


def explorer_flat(conn, category):
    """(columns, rows) for a flat Data Explorer table."""
    if category == "roster_note":
        rows = conn.execute(
            "SELECT n.on_date, n.note, u.display_name AS author "
            "FROM roster_note n LEFT JOIN users u ON u.id = n.author_id "
            "ORDER BY n.on_date").fetchall()
        return (["Date", "Note", "Entered by"],
                [[r["on_date"], r["note"], r["author"] or ""] for r in rows])
    if category == "service_start":
        rows = conn.execute(
            "SELECT a.tag, r.name, r.starts_on, r.occurred_on FROM asset_records r "
            "JOIN assets a ON a.id = r.asset_id "
            "WHERE r.category = 'service' AND r.starts_on IS NOT NULL "
            "ORDER BY a.tag, r.sort, r.name").fetchall()
        return (["Turbine", "Service", "Start date", "Completed"],
                [[r["tag"], r["name"], r["starts_on"], r["occurred_on"] or ""] for r in rows])
    raise ApiError(404, "Unknown table")


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

    def do_DELETE(self):
        self._dispatch("DELETE")

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
            role = user["role"]

            def can_see(m):
                if role in ("ADMIN", "VIEW"):        # VIEW sees everything an admin sees
                    return True
                if role == "CONTRACTOR":             # dashboard + asset information only
                    return m["key"] in ("dashboard", "assets")
                return m["min_role"] == "TECHNICIAN"

            return 200, {"modules": [dict(r) for r in rows if can_see(r)]}

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
                   "cu.display_name AS completed_by_name, uu.display_name AS updated_by_name "
                   "FROM pending_entries p JOIN assets a ON a.id = p.asset_id "
                   "JOIN users u ON u.id = p.author_id "
                   "LEFT JOIN users cu ON cu.id = p.completed_by "
                   "LEFT JOIN users uu ON uu.id = p.updated_by")
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
            for pt in conn.execute(
                "SELECT pt.pending_entry_id, pt.part_number, pt.quantity, pt.added_at, "
                "au.display_name AS added_by_name FROM pending_parts pt "
                "LEFT JOIN users au ON au.id = pt.added_by ORDER BY pt.id"):
                if pt["pending_entry_id"] in by_id:
                    by_id[pt["pending_entry_id"]]["parts"].append(
                        {"part_number": pt["part_number"], "quantity": pt["quantity"],
                         "added_at": pt["added_at"], "added_by_name": pt["added_by_name"]})
            for ph in conn.execute(
                "SELECT ph.pending_entry_id, ph.filename, ph.kind, ph.created_at, "
                "au.display_name AS added_by_name FROM pending_photos ph "
                "LEFT JOIN users au ON au.id = ph.added_by ORDER BY ph.id"):
                if ph["pending_entry_id"] in by_id:
                    by_id[ph["pending_entry_id"]]["photos"].append(
                        {"url": photo_url(ph["filename"]),
                         "thumb": photo_thumb_url(ph["filename"]), "kind": ph["kind"],
                         "created_at": ph["created_at"], "added_by_name": ph["added_by_name"]})
            counts = {s: 0 for s in ("SUBMITTED", "REVIEWED", "COMPLETED")}
            for r in conn.execute("SELECT status, COUNT(*) c FROM pending_entries GROUP BY status"):
                counts[r["status"]] = r["c"]
            return 200, {"pendings": rows, "counts": counts}

        # --- Notification Request ---
        if parts == ["notif"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            date = (query.get("date", [today()])[0] or today()).strip()
            return 200, load_notif(conn, date)

        if parts == ["notif"] and method == "POST":
            user = self._require(conn, "ADMIN")
            data = self._json_body()
            date = (data.get("date") or today()).strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                raise ApiError(400, "A roster date is required")
            op = data.get("op")

            if op == "submit":
                filed, cleared, submission_id = _notif_submit(conn, date, user)
                conn.commit()
                if submission_id and _outbox_delivery_method():
                    threading.Thread(target=deliver_outbox, daemon=True).start()
                res = load_notif(conn, date)
                res["filed"] = filed
                res["cleared"] = cleared
                res["submission_id"] = submission_id
                return 200, res

            team_no = int(data.get("team_no") or 0)
            if not (1 <= team_no <= NOTIF_MAX_TEAMS):
                raise ApiError(400, "team_no must be 1-%d" % NOTIF_MAX_TEAMS)
            row = conn.execute(
                "SELECT * FROM notif_request WHERE plan_date=? AND team_no=?", (date, team_no)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO notif_request (plan_date, team_no, created_at) VALUES (?,?,?)",
                    (date, team_no, now()))

            if op == "add_member":
                tech_id = data.get("tech_id")
                t = conn.execute(
                    "SELECT * FROM roster_tech WHERE id=? AND active=1", (tech_id,)
                ).fetchone()
                if not t:
                    raise ApiError(404, "Technician not found")
                off = conn.execute("SELECT code FROM roster_day WHERE tech_id=? AND on_date=?",
                                   (tech_id, date)).fetchone()
                if off and off["code"] not in ROSTER_AVAILABLE:
                    raise ApiError(409, "%s is unavailable (%s)" % (t["name"], off["code"]))
                n = conn.execute("SELECT COUNT(*) c FROM notif_member WHERE plan_date=? AND team_no=?",
                                 (date, team_no)).fetchone()["c"]
                if n >= NOTIF_TEAM_SIZE:
                    raise ApiError(409, "A team can hold at most %d technicians" % NOTIF_TEAM_SIZE)
                conn.execute(
                    "INSERT OR IGNORE INTO notif_member (plan_date, team_no, tech_id, slot) VALUES (?,?,?,?)",
                    (date, team_no, tech_id, n))
            elif op == "remove_member":
                conn.execute("DELETE FROM notif_member WHERE plan_date=? AND team_no=? AND tech_id=?",
                             (date, team_no, data.get("tech_id")))
            elif op == "set_field":
                field = data.get("field")
                if field not in ("asset_id", "contract_type", "description", "ats_case", "son"):
                    raise ApiError(400, "Unknown field")
                val = data.get("value")
                if field == "asset_id":
                    val = int(val) if val else None
                    if val and not conn.execute("SELECT 1 FROM assets WHERE id=?", (val,)).fetchone():
                        raise ApiError(404, "Turbine not found")
                elif field == "contract_type":
                    val = (val or "").strip() or None
                    if val and val not in CONTRACT_TYPES:
                        raise ApiError(400, "Unknown contract type")
                else:
                    val = (val or "").strip() or None
                conn.execute("UPDATE notif_request SET %s=? WHERE plan_date=? AND team_no=?" % field,
                             (val, date, team_no))
                # a contract type that isn't turbine-specific drops any turbine already picked
                if field == "contract_type" and val in NO_TURBINE_CONTRACTS:
                    conn.execute("UPDATE notif_request SET asset_id=NULL WHERE plan_date=? AND team_no=?",
                                 (date, team_no))
            elif op == "clear_team":
                conn.execute("DELETE FROM notif_member WHERE plan_date=? AND team_no=?", (date, team_no))
                conn.execute("DELETE FROM notif_request WHERE plan_date=? AND team_no=?", (date, team_no))
            else:
                raise ApiError(400, "Unknown op")
            return 200, load_notif(conn, date)

        if parts == ["notif", "export"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            date = (query.get("date", [today()])[0] or today()).strip()
            rows = _notif_export_rows(conn, date)
            if len(rows) < 2:
                raise ApiError(404, "No complete notification requests for %s" % date)
            return 200, RawResponse(
                build_xlsx([("Notification Requests", rows)]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "notification-requests-%s.xlsx" % date)

        # --- Notification Request outbox (delivery to the shared company workbook) ---
        if parts == ["notif", "outbox"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            limit = min(int((query.get("limit", ["300"])[0] or "300")), 1000)
            rows = conn.execute(
                "SELECT o.*, u.display_name AS created_by_name FROM notif_outbox o "
                "LEFT JOIN users u ON u.id = o.created_by "
                "ORDER BY o.id DESC LIMIT ?", (limit,)).fetchall()
            subs = {}
            for r in rows:
                s = subs.setdefault(r["submission_id"], {
                    "submission_id": r["submission_id"], "plan_date": r["plan_date"],
                    "created_at": r["created_at"], "created_by_name": r["created_by_name"],
                    "counts": {"PENDING": 0, "SENT": 0, "FAILED": 0, "SKIPPED": 0},
                    "rows": [],
                })
                s["counts"][r["status"]] = s["counts"].get(r["status"], 0) + 1
                s["rows"].append({
                    "id": r["id"], "team_no": r["team_no"], "contract_type": r["contract_type"],
                    "turbine": r["turbine"], "status": r["status"], "attempts": r["attempts"],
                    "last_error": r["last_error"], "sent_at": r["sent_at"], "sent_via": r["sent_via"],
                })
            submissions = sorted(subs.values(), key=lambda s: s["created_at"], reverse=True)
            for s in submissions:
                s["rows"].sort(key=lambda r: r["team_no"])
            return 200, {"submissions": submissions, "delivery_method": _outbox_delivery_method()}

        if parts == ["notif", "outbox"] and method == "POST":
            self._require(conn, "ADMIN")
            data = self._json_body()
            op = data.get("op")
            ids = [int(i) for i in (data.get("ids") or [])]
            if not ids:
                raise ApiError(400, "ids is required")
            qmarks = ",".join("?" * len(ids))
            if op == "retry":
                conn.execute(
                    "UPDATE notif_outbox SET status='PENDING' "
                    "WHERE id IN (%s) AND status IN ('FAILED','SKIPPED')" % qmarks, ids)
                conn.commit()
                if _outbox_delivery_method():
                    threading.Thread(target=deliver_outbox, daemon=True).start()
            elif op == "mark_sent":
                conn.execute(
                    "UPDATE notif_outbox SET status='SENT', sent_at=?, sent_via='manual', last_error=NULL "
                    "WHERE id IN (%s)" % qmarks, (now(), *ids))
            elif op == "discard":
                conn.execute(
                    "UPDATE notif_outbox SET status='SKIPPED' WHERE id IN (%s)" % qmarks, ids)
            else:
                raise ApiError(400, "Unknown op")
            row = conn.execute("SELECT submission_id FROM notif_outbox WHERE id = ?",
                               (ids[0],)).fetchone()
            counts = {r["status"]: r["c"] for r in conn.execute(
                "SELECT status, COUNT(*) c FROM notif_outbox WHERE submission_id = ? GROUP BY status",
                (row["submission_id"],))} if row else {}
            return 200, {"ok": True, "counts": counts}

        if parts == ["notif", "outbox", "export"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            sub = (query.get("submission_id", [""])[0] or "").strip()
            if not sub:
                raise ApiError(400, "submission_id is required")
            rows = conn.execute(
                "SELECT * FROM notif_outbox WHERE submission_id = ? ORDER BY team_no", (sub,)
            ).fetchall()
            if not rows:
                raise ApiError(404, "No such submission")
            out = [NOTIF_XLSX_HEADER] + [json.loads(r["cells"]) for r in rows]
            return 200, RawResponse(
                build_xlsx([("Notification Requests", out)]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "notification-requests-%s-%s.xlsx" % (rows[0]["plan_date"], sub))

        # --- Technician Roster ---
        if parts == ["roster"] and method == "GET":
            user = self._require(conn, ("ADMIN", "VIEW", "TECHNICIAN"))
            return 200, load_roster_month(conn, user, query)

        if parts == ["roster"] and method == "PATCH":
            self._require(conn, "ADMIN")
            data = self._json_body()
            tid = data.get("tech_id")
            d = (data.get("date") or "").strip()
            code = (data.get("code") or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                raise ApiError(400, "A date is required (YYYY-MM-DD)")
            if not conn.execute("SELECT 1 FROM roster_tech WHERE id = ? AND active = 1",
                                (tid,)).fetchone():
                raise ApiError(404, "Technician not found")
            if code and code not in ROSTER_KEY_CODES:
                raise ApiError(400, "Unknown roster code")
            if code:
                conn.execute(
                    "INSERT INTO roster_day (tech_id, on_date, code) VALUES (?,?,?) "
                    "ON CONFLICT(tech_id, on_date) DO UPDATE SET code = excluded.code",
                    (tid, d, code))
            else:
                conn.execute("DELETE FROM roster_day WHERE tech_id = ? AND on_date = ?", (tid, d))
            return 200, {"ok": True, "tech_id": tid, "date": d, "code": code}

        if parts == ["roster", "note"] and method == "PATCH":
            user = self._require(conn, "ADMIN")
            data = self._json_body()
            d = (data.get("date") or "").strip()
            note = (data.get("note") or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                raise ApiError(400, "A date is required (YYYY-MM-DD)")
            if note:
                conn.execute(
                    "INSERT INTO roster_note (on_date, note, author_id, created_at, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(on_date) DO UPDATE SET "
                    "note = excluded.note, author_id = excluded.author_id, updated_at = excluded.updated_at",
                    (d, note, user["id"], now(), now()))
            else:
                conn.execute("DELETE FROM roster_note WHERE on_date = ?", (d,))
            return 200, {"ok": True, "date": d, "note": note,
                         "author": user["display_name"] if note else None}

        if parts == ["roster", "train-note"] and method == "PATCH":
            user = self._require(conn)
            data = self._json_body()
            tid = data.get("tech_id")
            d = (data.get("date") or "").strip()
            note = (data.get("note") or "").strip()
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                raise ApiError(400, "A date is required (YYYY-MM-DD)")
            tech = conn.execute("SELECT id, user_id FROM roster_tech WHERE id = ?", (tid,)).fetchone()
            if not tech:
                raise ApiError(404, "Technician not found")
            if user["role"] not in ("ADMIN",) and tech["user_id"] != user["id"]:
                raise ApiError(403, "You can only note your own training days")
            if note:
                conn.execute(
                    "INSERT INTO roster_train_note (tech_id, on_date, note, author_id, updated_at) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(tech_id, on_date) DO UPDATE SET "
                    "note = excluded.note, author_id = excluded.author_id, updated_at = excluded.updated_at",
                    (tid, d, note, user["id"], now()))
            else:
                conn.execute("DELETE FROM roster_train_note WHERE tech_id = ? AND on_date = ?", (tid, d))
            return 200, {"ok": True, "tech_id": tid, "date": d, "note": note,
                         "author": user["display_name"] if note else None}

        if parts == ["roster", "techs"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            archived = query.get("archived", ["0"])[0] == "1"
            sql = ("SELECT t.id, t.name, t.active, t.archived_at, u.username AS linked_username "
                   "FROM roster_tech t LEFT JOIN users u ON u.id = t.user_id "
                   "WHERE t.is_contractor = 0 ")
            if not archived:
                sql += "AND t.active = 1 "
            sql += "ORDER BY t.active DESC, t.sort, t.name"
            free = conn.execute(
                "SELECT id, display_name, username, role FROM users WHERE active = 1 "
                "AND username <> 'admin' AND role <> 'CONTRACTOR' "
                "AND id NOT IN (SELECT user_id FROM roster_tech WHERE user_id IS NOT NULL) "
                "ORDER BY display_name").fetchall()
            return 200, {"techs": [dict(r) for r in conn.execute(sql)],
                         "free_accounts": [dict(r) for r in free]}

        if parts == ["roster", "techs"] and method == "POST":
            self._require(conn, "ADMIN")
            data = self._json_body()
            name = (data.get("name") or "").strip()
            if not name:
                raise ApiError(400, "A name is required")
            user_id = data.get("user_id") or None
            if user_id:
                if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone():
                    raise ApiError(404, "Account not found")
                if conn.execute("SELECT 1 FROM roster_tech WHERE user_id = ?", (user_id,)).fetchone():
                    raise ApiError(409, "That account is already a roster technician")
            mx = conn.execute("SELECT COALESCE(MAX(sort), 0) m FROM roster_tech").fetchone()["m"]
            conn.execute(
                "INSERT INTO roster_tech (name, user_id, active, sort, created_at) "
                "VALUES (?,?,1,?,?)", (name, user_id, mx + 1, now()))
            return 200, {"ok": True}

        if (len(parts) == 4 and parts[:2] == ["roster", "techs"]
                and parts[3] in ("archive", "restore") and method == "POST"):
            self._require(conn, "ADMIN")
            tid = parts[2]
            if not conn.execute("SELECT 1 FROM roster_tech WHERE id = ?", (tid,)).fetchone():
                raise ApiError(404, "Technician not found")
            if parts[3] == "archive":
                conn.execute("UPDATE roster_tech SET active = 0, archived_at = ? WHERE id = ?",
                             (now(), tid))
                conn.execute("DELETE FROM notif_member WHERE tech_id = ?", (tid,))
            else:
                conn.execute("UPDATE roster_tech SET active = 1, archived_at = NULL WHERE id = ?",
                             (tid,))
            return 200, {"ok": True}

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
                "SELECT id, category, name, occurred_on AS date, starts_on, detail, status, sort FROM asset_records "
                "WHERE asset_id = ? ORDER BY sort, name", (parts[1],),
            ).fetchall()
            history = conn.execute(
                "SELECT id, occurred_on AS date, description, work_type, service_order, technicians, source "
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

        # admin deletes a work-order / notification entry from an asset's history
        if len(parts) == 2 and parts[0] == "history" and method == "DELETE":
            self._require(conn, "ADMIN")
            row = conn.execute("SELECT id FROM asset_history WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "History entry not found")
            conn.execute("UPDATE notif_request SET history_id = NULL WHERE history_id = ?", (row["id"],))
            conn.execute("DELETE FROM asset_history WHERE id = ?", (row["id"],))
            return 200, {"ok": True}

        # set / clear a service, blade, hv or retrofit record date (admin only)
        if len(parts) == 2 and parts[0] == "records" and method == "PATCH":
            user = self._require(conn, "ADMIN")
            rec = conn.execute("SELECT * FROM asset_records WHERE id = ?", (parts[1],)).fetchone()
            if not rec:
                raise ApiError(404, "Record not found")
            if rec["category"] not in ("service", "blade", "hv", "retrofit"):
                raise ApiError(403, "This record type is not editable")
            body = self._json_body()
            field = "starts_on" if "starts_on" in body else "occurred_on"
            raw = (body.get(field) or "").strip()
            iso = None
            if raw:
                m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
                if not m:
                    raise ApiError(400, "Date must be YYYY-MM-DD")
                if not (2005 <= int(m.group(1)) <= 2040):
                    raise ApiError(400, "Date out of range")
                iso = raw
            old = rec[field]

            if field == "starts_on":
                conn.execute("UPDATE asset_records SET starts_on = ? WHERE id = ?", (iso, parts[1]))
                if (old or None) != (iso or None):
                    log_record_change(conn, rec, "starts_on", old, iso, user["id"])
                return 200, {"ok": True, "starts_on": iso}

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
            # the start date is kept on record (history / Data Explorer) even once
            # the service is completed — the asset tab just stops showing it.
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
                user = self._require(conn, ("ADMIN", "TECHNICIAN", "CONTRACTOR"))
                return self._create_pending(conn, asset_id, user)

        # --- pendings ---
        if parts == ["pendings", "review-count"] and method == "GET":
            self._require(conn)
            n = conn.execute(
                "SELECT COUNT(*) c FROM pending_entries WHERE status = 'SUBMITTED'"
            ).fetchone()["c"]
            return 200, {"submitted": n}

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
            # neither Submitted nor Reviewed is "done" — clear any completion record
            _CLR = "completed_note = NULL, completed_by = NULL, completed_at = NULL"
            if status == "REVIEWED":
                # a priority of 1-5 must be set before an entry can be reviewed
                pr = data.get("priority", row["priority"])
                try:
                    pr = int(pr)
                except (TypeError, ValueError):
                    pr = None
                if pr is None or not (1 <= pr <= 5):
                    raise ApiError(400, "Assign a priority of 1-5 before marking this entry Reviewed.")
                conn.execute("UPDATE pending_entries SET status = ?, priority = ?, " + _CLR + " WHERE id = ?",
                             (status, pr, parts[1]))
            else:  # leaving Reviewed drops any parts reservation
                conn.execute("UPDATE pending_entries SET status = ?, " + _CLR + ", "
                             "parts_service_order = NULL, parts_reserved_at = NULL WHERE id = ?",
                             (status, parts[1]))
                conn.execute("DELETE FROM pending_parts WHERE pending_entry_id = ?", (parts[1],))
            if row["status"] == "COMPLETED":
                conn.execute("DELETE FROM pending_photos WHERE pending_entry_id = ? AND kind = 'evidence'",
                             (parts[1],))
            return 200, {"ok": True}

        # admin reserves parts while an entry is Reviewed
        if len(parts) == 3 and parts[0] == "pendings" and parts[2] == "parts" and method == "POST":
            user = self._require(conn, "ADMIN")
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
                conn.execute("INSERT INTO pending_parts (pending_entry_id,part_number,quantity,added_by,added_at) "
                             "VALUES (?,?,?,?,?)", (parts[1], pn, qty, user["id"], now()))
            conn.execute("UPDATE pending_entries SET parts_service_order = ?, parts_reserved_at = ? "
                         "WHERE id = ?", (so or None, now(), parts[1]))
            return 200, {"ok": True}

        # admin OR technician adds a photo and/or a part to a Reviewed entry
        if len(parts) == 3 and parts[0] == "pendings" and parts[2] == "addition" and method == "POST":
            user = self._require(conn, ("ADMIN", "TECHNICIAN"))
            row = conn.execute("SELECT status FROM pending_entries WHERE id = ?", (parts[1],)).fetchone()
            if not row:
                raise ApiError(404, "Pending entry not found")
            if row["status"] != "REVIEWED":
                raise ApiError(409, "Photos and parts can only be added while the entry is Reviewed")
            fields, files = self._multipart()
            if fields is None:
                raise ApiError(400, "Expected a multipart form")
            pn = (fields.get("part_number") or "").strip()
            qty = (fields.get("quantity") or "").strip()
            has_photo = any(f["content"] for f in (files or []))
            if not pn and not has_photo:
                raise ApiError(400, "Add a photo, a part number, or both")
            saved = self._save_photos(conn, int(parts[1]), files, "reviewed", added_by=user["id"])
            if pn:
                conn.execute(
                    "INSERT INTO pending_parts (pending_entry_id,part_number,quantity,added_by,added_at) "
                    "VALUES (?,?,?,?,?)", (parts[1], pn, qty or "1", user["id"], now()))
            conn.execute("UPDATE pending_entries SET updated_by = ?, updated_at = ? WHERE id = ?",
                         (user["id"], now(), parts[1]))
            return 200, {"ok": True, "photos": saved, "part": bool(pn)}

        # technician (or admin) completes a Reviewed entry with a comment + evidence photo.
        # A technician must attach a photo; an admin may close without one.
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
            if not any(f["content"] for f in (files or [])) and user["role"] != "ADMIN":
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
                for k, lbl, vf in EXPLORER_CATEGORIES
            ] + [
                {"key": k, "label": lbl, "flat": True}
                for k, lbl in EXPLORER_FLAT.items()
            ]}

        if parts == ["explorer", "matrix"] and method == "GET":
            self._require(conn, ("ADMIN", "VIEW"))
            cat = (query.get("category", [""])[0] or "").strip()
            if cat in EXPLORER_FLAT:
                cols, rows = explorer_flat(conn, cat)
                return 200, {"category": cat, "label": EXPLORER_FLAT[cat], "flat": True,
                             "columns": cols, "rows": [{"cells": r} for r in rows]}
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
            buf = io.StringIO()
            w = csv.writer(buf)
            if cat in EXPLORER_FLAT:
                cols, rows = explorer_flat(conn, cat)
                w.writerow(cols)
                for r in rows:
                    w.writerow(r)
                fname = "explorer-%s-%s.csv" % (cat, today())
                return 200, RawResponse(buf.getvalue(), "text/csv; charset=utf-8", fname)
            if cat not in EXPLORER_VALUE_FIELD:
                raise ApiError(404, "Unknown category")
            vf = EXPLORER_VALUE_FIELD[cat]
            cols, by_tag = explorer_matrix(conn, cat, vf)
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

            nrows = conn.execute(
                "SELECT n.on_date, n.note, u.display_name AS author FROM roster_note n "
                "LEFT JOIN users u ON u.id = n.author_id "
                "WHERE n.on_date BETWEEN ? AND ? ORDER BY n.on_date", (frm, to)).fetchall()
            ndata = [["Date", "Note", "Entered by"]]
            for r in nrows:
                ndata.append([r["on_date"], (r["note"] or "").replace("\r\n", " ").replace("\n", " "),
                              r["author"] or ""])
            total += len(nrows)
            sheets.append(("Roster notes", ndata))

            srows = conn.execute(
                "SELECT a.tag, r.name, r.starts_on, r.occurred_on FROM asset_records r "
                "JOIN assets a ON a.id = r.asset_id "
                "WHERE r.category = 'service' AND r.starts_on BETWEEN ? AND ? "
                "ORDER BY r.starts_on, a.tag", (frm, to)).fetchall()
            sdata = [["Turbine", "Service", "Start date", "Completed"]]
            for r in srows:
                sdata.append([r["tag"], r["name"], r["starts_on"], (r["occurred_on"] or "")])
            total += len(srows)
            sheets.append(("Service start dates", sdata))

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

    def _save_photos(self, conn, entry_id, files, kind="note", limit=8, added_by=None):
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
                "INSERT INTO pending_photos (pending_entry_id,filename,caption,kind,added_by,created_at) "
                "VALUES (?,?,?,?,?,?)", (entry_id, fname, "", kind, added_by, now()),
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
        if not any(f["content"] for f in (files or [])):
            raise ApiError(400, "A photo is required to raise a new pending entry")
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


# Technician Roster -----------------------------------------------------------

# The Manplan "Key" — order here is the day-cell dropdown order.
ROSTER_KEY = [
    ["KILG", "Kilgallioch"], ["CARS", "Carscreugh"], ["BC", "Blackcraig"],
    ["HOL in WD", "Holiday (working day)"], ["HOL Apprvd", "Holiday approved"],
    ["TRG", "Training"], ["MED", "Medical"], ["SICK", "Sick"], ["ABS", "Absent"],
    ["COVID", "COVID"], ["SD", "Stand down"], ["ROST ON", "Rostered on"],
    ["ON CALL", "On call"], ["OFF", "Off"], ["PAT", "Paternity leave"],
    ["JURY", "Jury duty"],
]
ROSTER_KEY_CODES = {c for c, _ in ROSTER_KEY}
# a technician is available to a Notification Request only on a blank day or KILG
ROSTER_AVAILABLE = {"", "KILG"}
ROSTER_HOLIDAY_CODES = ("HOL in WD", "HOL Apprvd")


def _train_notes(conn, d0, d1, tech_id=None):
    """{str(tech_id): {date: {note, author, updated_at}}} for training-day notes."""
    q = ("SELECT n.tech_id, n.on_date, n.note, n.updated_at, u.display_name AS author "
         "FROM roster_train_note n LEFT JOIN users u ON u.id = n.author_id "
         "WHERE n.on_date BETWEEN ? AND ?")
    args = [d0, d1]
    if tech_id is not None:
        q += " AND n.tech_id = ?"
        args.append(tech_id)
    out = {}
    for r in conn.execute(q, args):
        out.setdefault(str(r["tech_id"]), {})[r["on_date"]] = {
            "note": r["note"], "author": r["author"], "updated_at": r["updated_at"]}
    return out


def load_roster_month(conn, user, query):
    """Roster calendar payload for one month. A TECHNICIAN sees only their own
    linked technician; ADMIN/VIEW may pass scope=team or scope=tech:<id>."""
    import calendar
    month = (query.get("month", [""])[0] or time.strftime("%Y-%m")).strip()
    if not re.match(r"^\d{4}-\d{2}$", month):
        month = time.strftime("%Y-%m")
    y, m = int(month[:4]), int(month[5:7])
    ndays = calendar.monthrange(y, m)[1]
    days = ["%04d-%02d-%02d" % (y, m, d) for d in range(1, ndays + 1)]

    my_tech = conn.execute(
        "SELECT id, name FROM roster_tech WHERE user_id = ? AND active = 1", (user["id"],)
    ).fetchone()

    scope = (query.get("scope", [""])[0] or "").strip()
    if user["role"] == "TECHNICIAN":
        scope = ("tech:%d" % my_tech["id"]) if my_tech else "none"
    elif not scope:
        scope = "team"

    if user["role"] == "TECHNICIAN":
        techs = [dict(my_tech)] if my_tech else []
    else:
        techs = [dict(r) for r in conn.execute(
            "SELECT id, name FROM roster_tech WHERE active = 1 AND is_contractor = 0 "
            "ORDER BY sort, name")]

    out = {"month": month, "days": days, "scope": scope,
           "can_edit": user["role"] == "ADMIN", "key": ROSTER_KEY,
           "can_note_train": user["role"] in ("ADMIN", "TECHNICIAN"),
           "techs": techs}

    if scope == "none":
        out["techs"] = []
        out["entries"] = {}
        return out

    if scope == "team":
        rows = conn.execute(
            "SELECT tech_id, on_date, code FROM roster_day WHERE on_date BETWEEN ? AND ?",
            (days[0], days[-1])).fetchall()
        ent = {}
        for r in rows:
            ent.setdefault(str(r["tech_id"]), {})[r["on_date"]] = r["code"]
        out["entries"] = ent
        out["train_notes"] = _train_notes(conn, days[0], days[-1])
        out["notes"] = {
            r["on_date"]: {"note": r["note"], "author": r["author"],
                           "updated_at": r["updated_at"]}
            for r in conn.execute(
                "SELECT n.on_date, n.note, n.updated_at, u.display_name AS author "
                "FROM roster_note n LEFT JOIN users u ON u.id = n.author_id "
                "WHERE n.on_date BETWEEN ? AND ?", (days[0], days[-1]))
        }
        return out

    mm = re.match(r"^tech:(\d+)$", scope)
    if not mm:
        raise ApiError(400, "Bad scope")
    tid = int(mm.group(1))
    trow = conn.execute("SELECT id, name FROM roster_tech WHERE id = ?", (tid,)).fetchone()
    if not trow:
        raise ApiError(404, "Technician not found")
    rows = conn.execute(
        "SELECT on_date, code FROM roster_day WHERE tech_id = ? AND on_date BETWEEN ? AND ?",
        (tid, days[0], days[-1])).fetchall()
    out["tech"] = dict(trow)
    out["entries"] = {str(tid): {r["on_date"]: r["code"] for r in rows}}
    out["train_notes"] = _train_notes(conn, days[0], days[-1], tid)
    # holidays: calendar year of the displayed month; sick: rolling 12 months from today
    yr = ("%04d-01-01" % y, "%04d-12-31" % y)
    te = datetime.date.fromisoformat(today())
    try:
        ts = te.replace(year=te.year - 1)
    except ValueError:                       # 29 Feb -> 28 Feb
        ts = te.replace(year=te.year - 1, day=28)
    sick = conn.execute(
        "SELECT COUNT(*) c FROM roster_day WHERE tech_id = ? AND code = 'SICK' "
        "AND on_date BETWEEN ? AND ?", (tid, ts.isoformat(), te.isoformat())).fetchone()["c"]
    hol = conn.execute(
        "SELECT COUNT(*) c FROM roster_day WHERE tech_id = ? AND code IN (?, ?) "
        "AND on_date BETWEEN ? AND ?", (tid, *ROSTER_HOLIDAY_CODES, *yr)).fetchone()["c"]
    out["totals"] = {"year": y, "holiday": hol, "sick": sick, "sick_since": ts.isoformat()}
    return out


# Notification Request ---------------------------------------------------------

NOTIF_TEAM_SIZE = 4          # technicians per team
NOTIF_MAX_TEAMS = 30
NOTIF_HUB = "SO5"           # matches the "Scott & Stuart" export ("SO5", letter O)
NOTIF_SITE = "Kilgallioch"

# Outbox delivery — two interchangeable transports, tried in this order:
#
# 1. Webhook: set NOTIF_OUTBOX_WEBHOOK to a URL (e.g. a Power Automate "When an
#    HTTP request is received" trigger that does "Add a row into a table") and
#    every submitted request is POSTed there as JSON. NOTIF_OUTBOX_API_KEY, if
#    set, is sent as the X-Api-Key header so the receiver can reject anything
#    else. (Needs a Premium Power Automate connector and can run into your
#    tenant's Data Loss Prevention policy — see 2 if that's blocked.)
# 2. Email: set NOTIF_OUTBOX_SMTP_HOST + NOTIF_OUTBOX_EMAIL_TO and every
#    request is emailed as a small JSON attachment to that address, subject
#    prefixed "NOTIF_ROW" — pair with a mail rule that files matching mail
#    into a folder, and a flow triggered on "When a new email arrives"
#    watching that folder (standard Outlook connector, no Premium, no DLP
#    conflict with Excel Online).
#
# Neither set: rows just queue until an admin marks them done by hand.
NOTIF_OUTBOX_WEBHOOK = os.environ.get("NOTIF_OUTBOX_WEBHOOK", "").strip()
NOTIF_OUTBOX_API_KEY = os.environ.get("NOTIF_OUTBOX_API_KEY", "").strip()
NOTIF_OUTBOX_SMTP_HOST = os.environ.get("NOTIF_OUTBOX_SMTP_HOST", "").strip()
NOTIF_OUTBOX_SMTP_PORT = int(os.environ.get("NOTIF_OUTBOX_SMTP_PORT", "587") or "587")
NOTIF_OUTBOX_SMTP_USER = os.environ.get("NOTIF_OUTBOX_SMTP_USER", "").strip()
NOTIF_OUTBOX_SMTP_PASS = os.environ.get("NOTIF_OUTBOX_SMTP_PASS", "").strip()
NOTIF_OUTBOX_EMAIL_FROM = os.environ.get("NOTIF_OUTBOX_EMAIL_FROM", "").strip() or NOTIF_OUTBOX_SMTP_USER
NOTIF_OUTBOX_EMAIL_TO = os.environ.get("NOTIF_OUTBOX_EMAIL_TO", "").strip()
NOTIF_OUTBOX_EMAIL_SUBJECT_PREFIX = "NOTIF_ROW"
NOTIF_OUTBOX_MAX_ATTEMPTS = 8


def _outbox_delivery_method():
    """Which transport is active, or None if rows just queue for manual review."""
    if NOTIF_OUTBOX_WEBHOOK:
        return "webhook"
    if NOTIF_OUTBOX_SMTP_HOST and NOTIF_OUTBOX_EMAIL_TO:
        return "email"
    return None

# Contract-type dropdown — mirrored from the "Lookups" sheet of the notification
# request workbook (column "Contract Type").
CONTRACT_TYPES = [
    "MINOR CORRECTIVE", "MAJOR CORRECTIVE", "SERVICE", "RETROFIT",
    "HV INSPECTIONS", "STAT INSPECTIONS", "OIL CHANGE", "HOSE CHANGE",
    "BLADE REPAIRS/INSPECTIONS", "INVERTER/DELTA EXCHANGES",
    "BILLABLE MINOR CORRECTIVE", "BILLABLE MAJOR CORRECTIVE", "BILLABLE RETROFIT",
    "BILLABLE ESCORTING", "WARRANTY MINOR CORRECTIVE", "WARRANTY MAJOR CORRECTIVE",
    "WARRANTY RETROFIT", "STORES - SERVICE", "STORES - CORRECTIVE",
    "SUPERVISOR DUTIES", "VEHICLE CHECK", "WEATHER/STAND DOWN", "GENERAL ADMIN",
]

# these contract types are not turbine-specific — no turbine number, and nothing
# is filed to an asset's history on submit.
NO_TURBINE_CONTRACTS = {
    "STORES - SERVICE", "STORES - CORRECTIVE", "SUPERVISOR DUTIES",
    "VEHICLE CHECK", "WEATHER/STAND DOWN", "GENERAL ADMIN",
}

# .xlsx column headers — must match the target sheet exactly for copy/paste.
NOTIF_XLSX_HEADER = [
    "DATE", "Hub", "SITE ", "TURBINE NO.", "FUNCTIONAL LOCATION", "FLOC & EQUIPMENT",
    "CONTRACT TYPE", "WBS ELEMENT (NSM)\nOr SALES DOC & LINE", "NOTIFICATION DESCRIPTION",
    "ATS Case", "SGRE NOTIFICATION NUMBER", "SGRE SERVICE ORDER NUMBER",
    "TECHNICIAN (LOCAL)", "TECHNICIAN (LOCAL)", "TECHNICIAN (LOCAL)",
    "TECHNICIAN (LOCAL)", "TECHNICIAN (LOCAL)", "TECHNICIAN (LOCAL)",
]


def _notif_teams(conn, date):
    """[{team_no, request_id, asset_id, asset_tag, contract_type, description,
        ats_case, submitted_at, members:[{id, display_name}]}], team order."""
    reqs = conn.execute(
        "SELECT r.*, a.tag AS asset_tag FROM notif_request r "
        "LEFT JOIN assets a ON a.id = r.asset_id "
        "WHERE r.plan_date = ? ORDER BY r.team_no", (date,)).fetchall()
    mem = conn.execute(
        "SELECT m.team_no, t.id, t.name AS display_name FROM notif_member m "
        "JOIN roster_tech t ON t.id = m.tech_id WHERE m.plan_date = ? "
        "ORDER BY m.slot, t.name", (date,)).fetchall()
    by_team = {}
    for m in mem:
        by_team.setdefault(m["team_no"], []).append(
            {"id": m["id"], "display_name": m["display_name"]})
    out = []
    for r in reqs:
        out.append({
            "team_no": r["team_no"], "request_id": r["id"],
            "asset_id": r["asset_id"], "asset_tag": r["asset_tag"],
            "contract_type": r["contract_type"], "description": r["description"],
            "ats_case": r["ats_case"], "son": r["son"], "submitted_at": r["submitted_at"],
            "members": by_team.get(r["team_no"], []),
        })
    return out


def _team_complete(t):
    """A request is ready once it has a contract type, a description and at least
    one technician. Every contract type needs a turbine too, except the six
    non-turbine ones (STORES / SUPERVISOR DUTIES / VEHICLE CHECK / WEATHER / GENERAL
    ADMIN) — those are export-only and filed nowhere; all others are saved to the
    turbine's history on submit."""
    if not (t["contract_type"] and (t["description"] or "").strip() and t["members"]):
        return False
    if t["contract_type"] in NO_TURBINE_CONTRACTS:
        return True
    return bool(t["asset_id"])


def load_notif(conn, date):
    """Everything the Notification Request screen needs for one roster date.
    Technicians and their availability come from the roster (roster_tech / roster_day)."""
    tech_rows = conn.execute(
        "SELECT t.id, t.name AS display_name, t.is_contractor, d.code AS reason "
        "FROM roster_tech t LEFT JOIN roster_day d ON d.tech_id = t.id AND d.on_date = ? "
        "WHERE t.active = 1 ORDER BY t.is_contractor, t.sort, t.name", (date,)).fetchall()

    teams = _notif_teams(conn, date)

    # a technician is "placed" once they sit on any team that date
    placed_teams = {}
    for t in teams:
        for m in t["members"]:
            placed_teams.setdefault(m["id"], []).append(t["team_no"])
    duplicates = [
        {"id": uid, "display_name": next(x["display_name"] for tt in teams
                                          for x in tt["members"] if x["id"] == uid),
         "teams": tns}
        for uid, tns in placed_teams.items() if len(tns) > 1
    ]

    available, contractors, unavailable = [], [], []
    for r in tech_rows:
        who = {"id": r["id"], "display_name": r["display_name"], "reason": None}
        if r["is_contractor"]:
            # contractors are always available; drop them from the box once placed
            if r["id"] not in placed_teams:
                contractors.append(who)
            continue
        code = r["reason"] or ""
        if code not in ROSTER_AVAILABLE:
            unavailable.append({**who, "reason": code})
        elif r["id"] not in placed_teams:
            available.append(who)

    return {
        "date": date,
        "hub": NOTIF_HUB,
        "site": NOTIF_SITE,
        "team_size": NOTIF_TEAM_SIZE,
        "teams": teams,
        "available": available,
        "contractors": contractors,
        "unavailable": unavailable,
        "placed_count": len(placed_teams),
        "duplicates": duplicates,
        "turbines": [dict(r) for r in conn.execute(
            "SELECT id, tag FROM assets ORDER BY tag")],
        "contract_types": CONTRACT_TYPES,
        "no_turbine_contracts": sorted(NO_TURBINE_CONTRACTS),
        "complete_count": sum(1 for t in teams if _team_complete(t)),
        "can_submit": any(_team_complete(t) for t in teams),
    }


_MONTH_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _notif_ddmmyyyy(date):
    # Spelled-out month (e.g. "04-Sep-2026") rather than "04/09/2026": a numeric
    # DD/MM date is ambiguous to Excel/Graph, which can read it back as MM/DD
    # regardless of the sheet's locale, silently swapping day and month.
    try:
        return "%s-%s-%s" % (date[8:10], _MONTH_ABBR[int(date[5:7]) - 1], date[0:4])
    except Exception:
        return date


def _notif_turbine_label(t):
    if t["contract_type"] in NO_TURBINE_CONTRACTS or not t["asset_tag"]:
        return ""
    return "%s %s" % (t["asset_tag"], NOTIF_SITE)                 # e.g. "B18 Kilgallioch"


def _notif_row_cells(t, date):
    """The 18 sheet cells for one complete team, in NOTIF_XLSX_HEADER column order."""
    names = [m["display_name"] for m in t["members"]][:NOTIF_TEAM_SIZE]
    names += [""] * (6 - len(names))
    return [
        _notif_ddmmyyyy(date), NOTIF_HUB, NOTIF_SITE, _notif_turbine_label(t),
        "", "", t["contract_type"], "",
        (t["description"] or "").strip(), (t["ats_case"] or "").strip(),
        "", (t["son"] or "").strip(), *names,      # SON -> SGRE SERVICE ORDER NUMBER
    ]


def _notif_row_payload(t, date):
    """Named-field view of one team's row — friendlier for a row-append action
    (Power Automate / Graph) than positional cells."""
    return {
        "date": _notif_ddmmyyyy(date),
        "iso_date": date,
        "hub": NOTIF_HUB,
        "site": NOTIF_SITE,
        "turbine_no": _notif_turbine_label(t),
        "functional_location": "",
        "floc_and_equipment": "",
        "contract_type": t["contract_type"] or "",
        "wbs_element": "",
        "notification_description": (t["description"] or "").strip(),
        "ats_case": (t["ats_case"] or "").strip(),
        "sgre_notification_number": "",
        "sgre_service_order_number": (t["son"] or "").strip(),
        "technicians": [m["display_name"] for m in t["members"]][:NOTIF_TEAM_SIZE],
    }


def _notif_export_rows(conn, date):
    """Header + one row per complete team, laid out like the target sheet."""
    rows = [NOTIF_XLSX_HEADER]
    for t in _notif_teams(conn, date):
        if _team_complete(t):
            rows.append(_notif_row_cells(t, date))
    return rows


def _outbox_enqueue(conn, date, user):
    """Queue every complete request for the date for delivery to the shared
    workbook. Returns (submission_id, n_rows). Called from _notif_submit before
    the board is cleared."""
    ready = [t for t in _notif_teams(conn, date) if _team_complete(t)]
    if not ready:
        return None, 0
    sub = secrets.token_hex(8)
    for t in ready:
        conn.execute(
            "INSERT INTO notif_outbox (submission_id, plan_date, team_no, contract_type, "
            "turbine, payload, cells, created_at, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
            (sub, date, t["team_no"], t["contract_type"], _notif_turbine_label(t),
             json.dumps(_notif_row_payload(t, date)),
             json.dumps(_notif_row_cells(t, date)), now(),
             user["id"] if user else None))
    return sub, len(ready)


def _notif_submit(conn, date, user=None):
    """Queue every complete request for delivery to the shared workbook, file the
    ones with a turbine into that turbine's history, then clear the whole board
    for the date. Incomplete draft teams are discarded.
    Returns (filed_to_history, requests, submission_id)."""
    teams = _notif_teams(conn, date)
    ready = [t for t in teams if _team_complete(t)]
    if not ready:
        raise ApiError(400, "Add at least one request — a contract type, a description "
                       "and at least one technician — before submitting.")
    sub, _ = _outbox_enqueue(conn, date, user)
    filed = 0
    for t in ready:
        if t["asset_id"] and t["contract_type"] not in NO_TURBINE_CONTRACTS:
            techs = ", ".join(m["display_name"] for m in t["members"])
            desc = (t["description"] or "").strip()
            if (t["ats_case"] or "").strip():
                desc += "  (ATS Case: %s)" % t["ats_case"].strip()
            conn.execute(
                "INSERT INTO asset_history (asset_id, occurred_on, description, work_type, "
                "service_order, technicians, source) VALUES (?,?,?,?,?,?,'notification')",
                (t["asset_id"], date, desc, t["contract_type"],
                 (t["son"] or "").strip() or None, techs))
            filed += 1
    conn.execute("DELETE FROM notif_member WHERE plan_date=?", (date,))
    conn.execute("DELETE FROM notif_request WHERE plan_date=?", (date,))
    return filed, len(ready), sub


# --- outbox delivery -------------------------------------------------------
#
# Decoupled from submit: a submit only writes PENDING rows. Delivery to the
# shared workbook happens here, best-effort, and retries on its own schedule so
# the external system being slow or down never blocks or fails a submit.

def _outbox_row_payload(row):
    return {
        "submission_id": row["submission_id"],
        "plan_date": row["plan_date"],
        "team_no": row["team_no"],
        "row": json.loads(row["payload"]),
        "cells": json.loads(row["cells"]),
        "columns": NOTIF_XLSX_HEADER,
    }


def _outbox_deliver_row_webhook(row):
    """POST one queued row to the configured webhook. Returns (ok, detail)."""
    body = json.dumps(_outbox_row_payload(row)).encode("utf-8")
    req = urllib.request.Request(NOTIF_OUTBOX_WEBHOOK, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if NOTIF_OUTBOX_API_KEY:
        req.add_header("X-Api-Key", NOTIF_OUTBOX_API_KEY)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            if 200 <= resp.status < 300:
                return True, "HTTP %d" % resp.status
            return False, "HTTP %d" % resp.status
    except urllib.error.HTTPError as e:
        return False, "HTTP %d %s" % (e.code, (e.reason or ""))
    except Exception as e:                                    # timeout, DNS, refused…
        return False, str(e) or e.__class__.__name__


def _outbox_deliver_row_email(row):
    """Email one queued row as a small JSON attachment. Returns (ok, detail).
    Paired with a mail rule that files matching subjects into a folder, and a
    flow triggered on new mail in that folder."""
    msg = EmailMessage()
    msg["Subject"] = "%s %s-%s" % (NOTIF_OUTBOX_EMAIL_SUBJECT_PREFIX, row["submission_id"], row["team_no"])
    msg["From"] = NOTIF_OUTBOX_EMAIL_FROM
    msg["To"] = NOTIF_OUTBOX_EMAIL_TO
    msg.set_content(
        "Notification Request row — plan date %s, team %s, %s.\n\n"
        "See the attached JSON for the full row; this email can be deleted "
        "once it's been picked up." % (row["plan_date"], row["team_no"], row["contract_type"] or ""))
    msg.add_attachment(
        json.dumps(_outbox_row_payload(row), indent=2).encode("utf-8"),
        maintype="application", subtype="json",
        filename="notification-%s-%s.json" % (row["submission_id"], row["team_no"]))
    try:
        with smtplib.SMTP(NOTIF_OUTBOX_SMTP_HOST, NOTIF_OUTBOX_SMTP_PORT, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if NOTIF_OUTBOX_SMTP_USER:
                s.login(NOTIF_OUTBOX_SMTP_USER, NOTIF_OUTBOX_SMTP_PASS)
            s.send_message(msg)
        return True, "sent to %s" % NOTIF_OUTBOX_EMAIL_TO
    except Exception as e:                                    # auth, timeout, DNS…
        return False, str(e) or e.__class__.__name__


def _outbox_deliver_row(row, method):
    if method == "webhook":
        return _outbox_deliver_row_webhook(row)
    if method == "email":
        return _outbox_deliver_row_email(row)
    return False, "no delivery method configured"


_outbox_lock = threading.Lock()


def deliver_outbox(limit=50):
    """Attempt delivery of PENDING / retryable FAILED rows. Safe to call from a
    worker thread or right after a submit. No-op when no transport is set.

    Guarded by a lock, with each row committed as it's sent: the immediate
    best-effort call after a submit and the periodic worker's tick can land at
    the same time, and without this a second caller could select the same
    still-uncommitted PENDING rows and send them again — duplicate emails, and
    duplicate rows in the workbook. If the lock is already held, this call is a
    safe no-op; whichever rows it would have picked up get caught next tick."""
    method = _outbox_delivery_method()
    if not method:
        return 0, 0
    if not _outbox_lock.acquire(blocking=False):
        return 0, 0
    try:
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT * FROM notif_outbox WHERE status IN ('PENDING','FAILED') "
                "AND attempts < ? ORDER BY id LIMIT ?",
                (NOTIF_OUTBOX_MAX_ATTEMPTS, limit)).fetchall()
            sent = failed = 0
            for row in rows:
                ok, detail = _outbox_deliver_row(row, method)
                if ok:
                    conn.execute(
                        "UPDATE notif_outbox SET status='SENT', sent_at=?, sent_via=?, "
                        "attempts=attempts+1, last_error=NULL WHERE id=?", (now(), method, row["id"]))
                    sent += 1
                else:
                    conn.execute(
                        "UPDATE notif_outbox SET status='FAILED', attempts=attempts+1, "
                        "last_error=? WHERE id=?", (detail[:500], row["id"]))
                    failed += 1
                conn.commit()          # per-row, so a concurrent caller sees fresh state ASAP
            return sent, failed
        finally:
            conn.close()
    finally:
        _outbox_lock.release()


def _outbox_worker():
    """Background retry loop. Started once from main() when a transport is set."""
    while True:
        time.sleep(120)
        try:
            deliver_outbox()
        except Exception as e:
            print("  outbox worker error: %s" % e)


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
                        "date": s.get("date") or s.get("occurred_on"),
                        "starts_on": s.get("starts_on")})
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
            "overdue": bool(planned and planned < today()),
            "starts_on": nxt.get("starts_on")}


def service_worklist(conn):
    """One row per turbine: its next incomplete service, ready to complete from the
    dashboard drill-down."""
    rows = conn.execute(
        "SELECT r.id, r.name, r.occurred_on, r.starts_on, a.id AS asset_id, a.tag, a.install_date "
        "FROM asset_records r JOIN assets a ON a.id = r.asset_id WHERE r.category = 'service'"
    ).fetchall()
    by_asset = {}
    for r in rows:
        g = by_asset.setdefault(r["asset_id"],
                                {"tag": r["tag"], "install": r["install_date"], "svc": []})
        g["svc"].append({"id": r["id"], "name": r["name"], "occurred_on": r["occurred_on"],
                         "starts_on": r["starts_on"]})
    out = []
    for aid, info in by_asset.items():
        n = next_service(info["svc"], info["install"])
        if n:
            out.append({"asset_id": aid, "tag": info["tag"], **n})
    out.sort(key=lambda x: (x["planned"] is None, x["planned"] or "", x["tag"]))
    return out


def load_pendings(conn, asset_id):
    rows = conn.execute(
        "SELECT p.*, u.display_name AS author_name, cu.display_name AS completed_by_name, "
        "uu.display_name AS updated_by_name "
        "FROM pending_entries p JOIN users u ON u.id = p.author_id "
        "LEFT JOIN users cu ON cu.id = p.completed_by "
        "LEFT JOIN users uu ON uu.id = p.updated_by "
        "WHERE p.asset_id = ? "
        "ORDER BY CASE p.status WHEN 'SUBMITTED' THEN 0 WHEN 'REVIEWED' THEN 1 ELSE 2 END, "
        "p.created_at DESC",
        (asset_id,),
    ).fetchall()
    out = []
    for r in rows:
        photos = conn.execute(
            "SELECT ph.id, ph.filename, ph.caption, ph.kind, ph.created_at, "
            "au.display_name AS added_by_name "
            "FROM pending_photos ph LEFT JOIN users au ON au.id = ph.added_by "
            "WHERE ph.pending_entry_id = ? ORDER BY ph.id", (r["id"],),
        ).fetchall()
        parts = conn.execute(
            "SELECT pt.part_number, pt.quantity, pt.added_at, au.display_name AS added_by_name "
            "FROM pending_parts pt LEFT JOIN users au ON au.id = pt.added_by "
            "WHERE pt.pending_entry_id = ? ORDER BY pt.id", (r["id"],),
        ).fetchall()
        d = dict(r)
        d["photos"] = [{"id": p["id"], "url": photo_url(p["filename"]),
                        "thumb": photo_thumb_url(p["filename"]),
                        "caption": p["caption"], "kind": p["kind"],
                        "added_by_name": p["added_by_name"],
                        "created_at": p["created_at"]} for p in photos]
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
                for t in ("pending_photos", "record_changes", "notif_request", "notif_outbox"))
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
    # Line-buffer stdout so the startup banner / migration lines reach hosted logs
    # even without $PYTHONUNBUFFERED.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
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
    n_smp = conn.execute("SELECT COUNT(*) c FROM assets WHERE smp_gearbox IS NOT NULL").fetchone()["c"]
    n_outbox_pending = conn.execute(
        "SELECT COUNT(*) c FROM notif_outbox WHERE status IN ('PENDING','FAILED')").fetchone()["c"]
    conn.close()

    if _outbox_delivery_method():
        threading.Thread(target=_outbox_worker, daemon=True).start()
        deliver_outbox()          # drain anything queued while the server was down

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    url = "http://%s:%d" % ("localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host, args.port)
    print("\n  Site Portal")
    print("  " + "-" * 40)
    print("  running at   %s" % url)
    print("  logins       from source/Credentials.csv (synced each start)")
    print("  condition    SMP state for %d turbines from source/KGH_SMP.csv (synced each start)" % n_smp)
    print("  break-glass  admin / %s" % (os.environ.get("ADMIN_PASSWORD") or "admin123"))
    print("  database     %s  (%s)" % (DB_PATH, "freshly seeded" if fresh else "existing, kept"))
    print("  contents     %d pending entries (%d added in-app), %d logged record edits"
          % (n_pend, n_app, n_edits))
    print("  photos       %s" % ("Pillow ready — uploads resized to %dpx, thumbnails on"
                                 % PHOTO_MAX_EDGE if _HAVE_PIL
                                 else "Pillow NOT installed — photos stored as-is (pip install pillow)"))
    print("  note         app-entered data lives in data/ — plain `python3 app.py` keeps it;")
    print("               `--reset` deletes it. Nothing else wipes the DB.")
    _outbox_desc = {
        "webhook": "delivering via webhook to " + NOTIF_OUTBOX_WEBHOOK,
        "email": "delivering via email to " + NOTIF_OUTBOX_EMAIL_TO,
        None: "no delivery method set — rows queue for manual review",
    }[_outbox_delivery_method()]
    print("  notif outbox %s%s" % (
        _outbox_desc, (", %d queued" % n_outbox_pending) if n_outbox_pending else ""))
    print("  stop         Ctrl-C\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")


if __name__ == "__main__":
    main()
