"""
Parse the source CSV exports (one per whiteboard tab) into the structures the
app seeds from. One CSV per source spreadsheet lives in ``source/``.

Call ``load()`` to get a dict with everything the seeder needs. Standard library only.
"""

import csv
import datetime
import os
import re

SOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source")
ARCHIVE_DIR = os.path.join(SOURCE_DIR, "_archived")

# Files in source/ that are re-synced into the database on every start (live config /
# periodically-refreshed external data):
#   Credentials.csv  -> the users table
#   KGH_SMP.csv      -> the condition-monitoring (SMP) columns on assets
CRED_FILE = "Credentials.csv"
SMP_FILE = "KGH_SMP.csv"
# The one-time turbine/asset import — these live in source/_archived/ and are read
# only to rebuild a completely empty database.
DATA_FILES = {
    "kgh2025": "KGH_2025.csv",
    "svcdates": "KGH_Service_Dates.csv",
    "retro": "25_KGH_Retro.csv",
    "hv": "HV.csv",
    "stats": "Stats.csv",
    "equipment": "Equipment_info.csv",
    "components": "Kilgallioch_App_data.csv",
    "manplan": "Manplan.csv",
    "jobreq": "Job_Request.csv",
    "pendings": "Pendings.csv",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_BAD = {"", "#REF!", "#N/A", "N/A", "NA", "-", "--", "TBC", "TBA", "FALSE",
        "TRUE", "OK", "NONE", "?", "N/A ", "NA "}


def _rows(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return [[c.strip() for c in row] for row in csv.reader(fh)]


def _data_rows(key):
    path = os.path.join(ARCHIVE_DIR, DATA_FILES[key])
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "%s is missing. The one-time import CSVs live in source/_archived/ and "
            "are only needed to rebuild an empty database." % path)
    return _rows(path)


def _get(row, i):
    return row[i].strip() if 0 <= i < len(row) and row[i] is not None else ""


def clean(v):
    v = (v or "").strip()
    return None if v.upper() in {b.upper() for b in _BAD} else v


def parse_date(s):
    """Accept ISO (YYYY-MM-DD), DD/MM/YYYY, or 'Wednesday, 7 January 2026'."""
    s = (s or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
        if m:
            d, mo, y = (int(x) for x in m.groups())
        else:
            for fmt in ("%A, %d %B %Y", "%A %d %B %Y", "%d %B %Y", "%d-%b-%Y"):
                try:
                    return datetime.datetime.strptime(s, fmt).date().isoformat()
                except ValueError:
                    continue
            return None
    if not (2005 <= y <= 2035):
        return None
    try:
        return datetime.date(y, mo, d).isoformat()
    except ValueError:
        return None


def norm_tag(raw):
    """'A1' / 'a01' / 'D-32' / 'B18 Kilgallioch' -> 'A01'."""
    if not raw:
        return None
    tok = raw.strip().split()[0]
    m = re.match(r"^([A-Za-z]+)[-\s]?0*(\d+)$", tok)
    return "%s%02d" % (m.group(1).upper(), int(m.group(2))) if m else None


# ---------------------------------------------------------------------------
# per-tab parsers  (each CSV has a single header row; data starts at row 1)
# ---------------------------------------------------------------------------

def _kgh2025(rows):
    """KGH_2025.csv -> (turbine order, defect notes, blade-inspection records).

    Service dates come from KGH_Service_Dates.csv now — see _service_dates()."""
    BLADE = [
        (57, "Blade drone inspection"),
    ]
    order, defects, blades = [], {}, {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        order.append(tag)
        blades[tag] = [
            {"name": nm, "date": parse_date(_get(row, ci)), "detail": None, "sort": si}
            for si, (ci, nm) in enumerate(BLADE)
        ]
        defects[tag] = clean(_get(row, 6))  # "Defects / Issues affecting work and/or operation"
    return order, defects, blades


# The comprehensive service schedule, in order. Column 0 of KGH_Service_Dates.csv is
# the turbine tag; columns 1.. are these services. A cell holds either a completion
# date or a status literal ('Not required' / 'Not Completed' / 'TBC'), which is kept
# verbatim as the record's detail.
_SVC_DATES = [
    "3 Month Service - Tower and Nacelle", "3 Month Service - Rotor",
    "3 Month Service - Foundation and TX", "6 Month Major Service",
    "12 Month Major Service", "18 Month Minor Service", "24 Month Major Service",
    "30 Month Minor Service", "36 Month Major Service", "42 Month Minor Service",
    "48 Month Major Service", "54 Month Minor Service", "60 Month Major Service",
    "66 Month Minor Service", "72 Month Major Service", "78 Month Minor Service",
    "84 Month Major Service", "90 Month Major Service", "96 Month Major Service",
    "102 Month Minor Service", "108 Month Major Service", "114 Month Major Service",
    "120 Month Major Service", "126 Month Major Service", "132 Month Major Service",
    "5-Year Oil Exchange", "10-Year Oil Exchange",
]


def _service_dates(rows):
    """KGH_Service_Dates.csv -> {tag: [{name, date, detail, sort}, ...]}."""
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = []
        for i, name in enumerate(_SVC_DATES):
            raw = _get(row, i + 1)
            date = parse_date(raw)
            recs.append({
                "name": name,
                "date": date,
                "detail": None if (date or not raw) else raw,
                "sort": i,
            })
        out[tag] = recs
    return out


def _hv(rows):
    #  name, completed_col, planned_col (None = no source column -> always Null), sort
    DEFS = [
        ("HV maintenance - 2023 campaign", 2, None, 0),
        ("HV maintenance - 2024/25 rephasing", 7, 6, 1),
        ("HV maintenance - 2025/26", 10, 9, 2),
        ("HV maintenance - 2026/27", None, None, 3),
        ("HV maintenance - 2027/28", None, None, 4),
    ]
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        scope = clean(_get(row, 4))
        recs = []
        for name, ci, pi, si in DEFS:
            date = parse_date(_get(row, ci)) if ci is not None else None
            planned = parse_date(_get(row, pi)) if pi is not None else None
            detail = None
            if si == 0 and scope:
                detail = scope + " scope"
            elif planned and not date:
                detail = "Planned " + planned
            recs.append({"name": name, "date": date, "detail": detail, "sort": si})
        out[tag] = recs
    return out


def _stats(rows):
    DEFS = [
        ("Annual stat inspection - 2024", 3, 0),
        ("Semi-annual stat inspection - 2024", 6, 1),
        ("Annual stat inspection - 2025", 11, 2),
        ("Semi-annual stat inspection - 2025", 15, 3),
        ("Annual stat inspection - 2026", 24, 4),
        ("10-year lift inspection", 26, 5),
    ]
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = [
            {"name": nm, "date": parse_date(_get(row, ci)), "detail": None, "sort": si}
            for nm, ci, si in DEFS if parse_date(_get(row, ci))
        ]
        if recs:
            out[tag] = recs
    return out


def _retrofits(rows):
    # (name, completed_col, started_col_or_None, sort)
    DEFS = [
        ("Slipring Exhaust (B25152100)", 3, None, 0),
        ("Lightning Rod (B25372100)", 6, None, 1),
        ("Nacelle Roof (B0132105)", 9, None, 2),
        ("Spinner Hatch Containment", 12, None, 3),
        ("Nacelle Ladder Plate (B0052000)", 15, None, 4),
        ("Door Striker (B33122001)", 18, None, 5),
        ("External Bracket (B0322000)", 21, None, 6),
        ("Hoist Chainbag Suspension Mod (B0142200)", 24, None, 7),
        ("Generator Duct Replacement", 30, None, 8),
        ("Nose Cone Retro (B0132100/B0132102)", 33, None, 9),
        ("Rotor Cover V4 Rear Supports (B25132103)", 37, 36, 10),
        ("Pitch Retro (SL)", 41, 40, 11),
        ("Hub Steps (B0062200)", 46, None, 12),
        ("Descender", 51, None, 13),
        ("Fire Extinguisher - Blade", 52, None, 14),
        ("Fire Extinguisher - Nacelle", 53, None, 15),
        ("Tower Platform Works", 56, 55, 16),
        ("Mainshaft Bearing PT Sensors (B25091701)", 60, 59, 17),
        ("TX Wall Gap Retrofit (B25182100)", 68, 67, 18),
        ("Transformer Wall Upgrade (Bundled with HV Maintenance)", 73, None, 19),
    ]
    DONE = {"Y", "YES", "DONE", "COMPLETE", "COMPLETED"}
    SKIP = {"N/A", "NA", "--", "-", "#N/A", "NONE", "", "FALSE"}
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = []
        for name, ci, si_col, sort in DEFS:
            raw = _get(row, ci)
            started = _get(row, si_col) if si_col is not None else ""
            date = parse_date(raw)
            if date:
                status, dt = "complete", date
            elif raw.upper() in DONE:
                status, dt = "complete", None
            elif raw.upper() in SKIP and not parse_date(started):
                if name.startswith("Generator Duct") and _get(row, 29).lower().startswith("ok"):
                    continue
                if raw.upper() in {"N/A", "NA", "--", "-", "#N/A", "NONE"}:
                    continue
                status, dt = "outstanding", None
            elif parse_date(started):
                status, dt = "in_progress", None
            else:
                status, dt = "outstanding", None
            recs.append({"name": name, "status": status, "date": dt, "sort": sort})
        out[tag] = recs
    return out


def _components(rows):
    FIELDS = [
        ("Nacelle S/N", 1, 0), ("Gearbox S/N", 2, 1), ("Generator S/N", 3, 2),
        ("Transformer S/N", 4, 3), ("Ground cabinet S/N", 5, 4), ("Hub S/N", 10, 5),
        ("Tower section T1 S/N", 6, 6), ("Tower section T2 S/N", 7, 7),
        ("Tower section T3 S/N", 8, 8), ("Tower section T4 S/N", 9, 9),
        ("Blade type", 14, 10), ("Blade A S/N", 11, 11), ("Blade B S/N", 12, 12),
        ("Blade C S/N", 13, 13), ("Blade bearing manufacturer", 16, 14),
        ("Blade bearing A S/N", 17, 15), ("Blade bearing B S/N", 18, 16),
        ("Blade bearing C S/N", 19, 17),
    ]
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = []
        comm = parse_date(_get(row, 15))
        if comm:
            recs.append({"name": "Commissioned", "date": comm, "detail": None, "sort": -1})
        for name, ci, sort in FIELDS:
            val = clean(_get(row, ci))
            if val:
                recs.append({"name": name, "date": None, "detail": val, "sort": sort})
        out[tag] = recs
    return out


def _equipment(rows):
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        out[tag] = {
            "manufacturer": clean(_get(row, 1)), "model": clean(_get(row, 2)),
            "family": clean(_get(row, 3)), "serial": clean(_get(row, 4)),
            "install_date": parse_date(_get(row, 5)), "toc": parse_date(_get(row, 6)),
            "warranty_expiry": parse_date(_get(row, 7)),
        }
    return out


def _smp(rows):
    """source/KGH_SMP.csv: Turbine, Data date, State Gearbox, State Generator,
    State Main Bearing, Observations. Re-synced into the assets table every start."""
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        out[tag] = {
            "data_date": parse_date(_get(row, 1)),
            "gearbox": _get(row, 2) or None,
            "generator": _get(row, 3) or None,
            "main_bearing": _get(row, 4) or None,
            "observations": _get(row, 5) or None,
        }
    return out


# The Manplan "Key" — recognised roster codes (uppercased for matching).
_ROSTER_CODES = {
    "KILG", "CARS", "BC", "HOL IN WD", "HOL APPRVD", "TRG", "MED", "SICK",
    "ABS", "COVID", "SD", "ROST ON", "ON CALL", "OFF", "PAT", "JURY",
}
_ROSTER_CANON = {"HOL IN WD": "HOL in WD", "HOL APPRVD": "HOL Apprvd"}


def _roster_code(raw):
    """Normalise a Manplan cell to a Key code (canonical casing), or None."""
    c = (raw or "").strip().upper()
    if not c:
        return None
    if c == "HOL":
        c = "HOL IN WD"
    if c in _ROSTER_CODES:
        return _ROSTER_CANON.get(c, c)
    return None


def _manplan(rows):
    """Manplan.csv -> {name: {iso_date: code}} for the technician rows (13-36).
    Row 11 holds the dates; column 4 of each tech row holds the name."""
    date_row = rows[11] if len(rows) > 11 else []
    col_date = {i: iso for i, v in enumerate(date_row) if (iso := parse_date(v))}
    grid = {}
    for row in rows[13:37]:
        name = _get(row, 4)
        if not name:
            continue
        days = {}
        for ci, iso in col_date.items():
            code = _roster_code(_get(row, ci))
            if code:
                days[iso] = code
        grid[name] = days
    return grid


def _jobreq(rows):
    """Job_Request.csv - col 4 = clean turbine, 7 = contract type, 9 = description,
       12 = service order, 13-18 = technicians."""
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 4) or _get(row, 3))
        if not tag:
            continue
        techs = [t for t in (_get(row, i) for i in range(13, 19)) if t and t != "-"]
        wt = _get(row, 7)
        out.setdefault(tag, []).append({
            "date": parse_date(_get(row, 0)),
            "description": _get(row, 9) or wt or "Work order",
            "work_type": (wt.title().replace("Hv", "HV")) or None,
            "service_order": (_get(row, 12) if _get(row, 12) not in ("", "-") else None),
            "technicians": ", ".join(techs) or None,
        })
    for tag in out:
        out[tag].sort(key=lambda e: e["date"] or "", reverse=True)
    return out


def _credentials(rows):
    """Credentials.csv -> login accounts. Columns: Name, First name, Username, Access, Password.

    Access: 'Admin' -> ADMIN (full edit), 'View' -> VIEW (read-only, sees everything
    an admin sees), anything else -> TECHNICIAN (can only add / complete pendings).
    """
    out = []
    for row in rows[1:]:
        name = _get(row, 0)
        username = _get(row, 2)
        access = _get(row, 3).upper()
        password = _get(row, 4)
        if not username or not password:
            continue
        role = "ADMIN" if access.startswith("ADMIN") else \
               "VIEW" if access.startswith("VIEW") else "TECHNICIAN"
        out.append({
            "name": name or username,
            "username": username,
            "role": role,
            "password": password,
        })
    return out


def _pendings(rows):
    """Pendings.csv -> list of pending entries per turbine.
       col 0 WO code, 3 turbine, 4 priority, 5 description, 6 long desc,
       7 detected (CREA/APR), 14 note, 17 system, 28 notification date."""
    STATUS = {"CREA": "SUBMITTED", "APR": "REVIEWED"}
    out = []
    for row in rows[1:]:
        if not any(c for c in row):
            continue
        tag = norm_tag(_get(row, 3))
        if not tag:
            continue
        desc = _get(row, 5)
        parts = [p for p in (desc, _get(row, 6), _get(row, 14)) if p]
        seen, note_parts = set(), []
        for p in parts:
            key = p.lower().strip()
            if key and key not in seen:
                seen.add(key)
                note_parts.append(p)
        note = "\n\n".join(note_parts) or "(no description)"
        try:
            priority = int(_get(row, 4))
        except ValueError:
            priority = None
        out.append({
            "tag": tag,
            "wo_code": _get(row, 0) or None,
            "priority": priority,
            "system": clean(_get(row, 17)),
            "note": note,
            "status": STATUS.get(_get(row, 7).upper(), "SUBMITTED"),
            "created_at": parse_date(_get(row, 28)) or parse_date(_get(row, 10)),
        })
    return out


# ---------------------------------------------------------------------------
# public entry points
# ---------------------------------------------------------------------------

def load_credentials():
    """source/Credentials.csv -> login accounts. Read on every server start."""
    return _credentials(_rows(os.path.join(SOURCE_DIR, CRED_FILE)))


def load_smp():
    """source/KGH_SMP.csv -> condition-monitoring state per turbine. Re-synced every start."""
    return _smp(_rows(os.path.join(SOURCE_DIR, SMP_FILE)))


def load_service_dates():
    """The comprehensive service schedule. Applied once (see app._replace_service_records);
    after that, service completions are edited in the app."""
    return _service_dates(_data_rows("svcdates"))


def load_data():
    """The one-time turbine / asset / history import from source/_archived/*.csv.
    Only called when the database is completely empty."""
    order, defects, blades = _kgh2025(_data_rows("kgh2025"))
    return {
        "turbines": order,
        "services": _service_dates(_data_rows("svcdates")),
        "defects": defects,
        "blades": blades,
        "hv": _hv(_data_rows("hv")),
        "stat": _stats(_data_rows("stats")),
        "retrofits": _retrofits(_data_rows("retro")),
        "components": _components(_data_rows("components")),
        "equipment": _equipment(_data_rows("equipment")),
        "roster": _manplan(_data_rows("manplan")),   # {name: {iso: code}}
        "history": _jobreq(_data_rows("jobreq")),
        "pendings": _pendings(_data_rows("pendings")),
    }


def load_roster():
    """source/_archived/Manplan.csv -> {name: {iso_date: code}}. One-time import;
    after the first boot the roster_day table is the live record."""
    return _manplan(_data_rows("manplan"))


def load():  # full set — used by the sanity dump below
    return {**load_data(), "credentials": load_credentials(), "smp": load_smp()}


if __name__ == "__main__":  # quick sanity dump
    d = load()
    print("turbines:", len(d["turbines"]), d["turbines"][:5])
    for k in ("services", "hv", "stat", "retrofits", "components", "equipment", "smp", "history"):
        print(f"{k}: {len(d[k])} turbines")
    print("roster: %d techs, %d day entries" %
          (len(d["roster"]), sum(len(v) for v in d["roster"].values())))
    print("defects with text:", sum(1 for v in d["defects"].values() if v))
    print("pendings:", len(d["pendings"]),
          "| turbines:", len({p['tag'] for p in d['pendings']}))
    print("A01 defect:", d["defects"].get("A01"))
    print("A01 hv:", d["hv"].get("A01"))
    print("A01 retro not-complete:",
          [r["name"] for r in d["retrofits"].get("A01", []) if r["status"] != "complete"])
    print("first pending:", d["pendings"][0])
    print("A01 history[0]:", d["history"].get("A01", [None])[0])
