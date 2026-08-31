"""
Parse the source CSV exports (one per whiteboard tab) into the structures the
app seeds from. One CSV per Excel tab lives in ``source/``.

Call ``load()`` to get a dict with everything the seeder needs. Standard library only.
"""

import csv
import datetime
import os
import re

SOURCE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "source")

FILES = {
    "kgh2025": "KGH 2025.csv",
    "retro": "25 KGH Retro.csv",
    "hv": "HV.csv",
    "stats": "Stats.csv",
    "smp": "KGH SMP Action Tracker.csv",
    "equipment": "Equipment info.csv",
    "components": "Kilgallioch App data.csv",
    "manplan": "Manplan 2025.csv",
    "jobreq": "Job Request.csv",
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_BAD = {"", "#REF!", "#N/A", "N/A", "NA", "-", "--", "TBC", "TBA", "FALSE",
        "TRUE", "OK", "NONE", "?", "N/A ", "NA "}


def _rows(name):
    with open(os.path.join(SOURCE_DIR, name), newline="", encoding="utf-8-sig") as fh:
        return [[c.strip() for c in row] for row in csv.reader(fh)]


def _get(row, i):
    return row[i].strip() if i < len(row) and row[i] is not None else ""


def clean(v):
    v = (v or "").strip()
    return None if v.upper() in {b.upper() for b in _BAD} else v


def d_dmy(s):
    """DD/MM/YYYY -> ISO, rejecting junk and pre-2000 (mis-formatted serials)."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if not m:
        return None
    dd, mm, yy = (int(x) for x in m.groups())
    if yy < 2005 or yy > 2035:
        return None
    try:
        return datetime.date(yy, mm, dd).isoformat()
    except ValueError:
        return None


def d_mdy(s):
    """M/D/YYYY (SMP 'Last Data Date')."""
    s = (s or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if not m:
        return None
    mm, dd, yy = (int(x) for x in m.groups())
    try:
        return datetime.date(yy, mm, dd).isoformat()
    except ValueError:
        return None


def d_long(s):
    """'Wednesday, 7 January 2026' -> ISO."""
    s = (s or "").strip()
    for fmt in ("%A, %d %B %Y", "%A %d %B %Y", "%d %B %Y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def norm_tag(raw):
    """'A1' / 'a01' / 'B18 Kilgallioch' -> 'A01'."""
    if not raw:
        return None
    tok = raw.strip().split()[0]
    m = re.match(r"^([A-Za-z]+)0*(\d+)$", tok)
    return "%s%02d" % (m.group(1).upper(), int(m.group(2))) if m else None


def make_username(full, taken):
    parts = "".join(ch for ch in full.lower() if ch.isalpha() or ch.isspace()).split()
    base = (parts[0][0] + parts[-1]) if len(parts) > 1 else (parts[0] if parts else "user")
    uname, i = base, 1
    while uname in taken:
        i += 1
        uname = "%s%d" % (base, i)
    taken.add(uname)
    return uname


# ---------------------------------------------------------------------------
# per-tab parsers
# ---------------------------------------------------------------------------

def _turbines_and_services(rows):
    """KGH 2025.csv - row 1 headers, rows 2+ data. Returns (order, {tag: [service recs]})."""
    SVC = [
        (9, "72 Month Major Service"), (13, "84 Month Major Service"),
        (19, "90 Month Major Service"), (25, "96 Month Major Service"),
        (30, "102 Month Minor Service"), (35, "108 Month Major Service"),
        (41, "114 Month Major Service"),
    ]
    order, services = [], {}
    for row in rows[2:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        order.append(tag)
        services[tag] = [
            {"name": nm, "date": d_dmy(_get(row, ci)), "detail": None, "sort": si}
            for si, (ci, nm) in enumerate(SVC)
        ]
    return order, services


def _hv(rows):
    """HV.csv - row 1 headers, rows 2+ data."""
    DEFS = [
        ("HV maintenance - 2023 campaign", 2, None, 0),
        ("HV maintenance - 2024/25 rephasing", 7, 6, 1),
        ("HV maintenance - 2025/26", 10, 9, 2),
    ]
    out = {}
    for row in rows[2:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        scope = clean(_get(row, 4))
        recs = []
        for name, ci, pi, si in DEFS:
            date = d_dmy(_get(row, ci))
            planned = d_dmy(_get(row, pi)) if pi is not None else None
            if not date and not planned:
                continue
            detail = None
            if si == 0 and scope:
                detail = scope + " scope"
            elif planned and not date:
                detail = "Planned " + planned
            recs.append({"name": name, "date": date, "detail": detail, "sort": si})
        if recs:
            out[tag] = recs
    return out


def _stats(rows):
    """Stats.csv - row 1 headers, rows 2+ data."""
    DEFS = [
        ("Annual stat inspection - 2024", 3, 0),
        ("Semi-annual stat inspection - 2024", 6, 1),
        ("Annual stat inspection - 2025", 11, 2),
        ("Semi-annual stat inspection - 2025", 15, 3),
        ("Annual stat inspection - 2026", 24, 4),
        ("10-year lift inspection", 26, 5),
    ]
    out = {}
    for row in rows[2:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = [
            {"name": nm, "date": d_dmy(_get(row, ci)), "detail": None, "sort": si}
            for nm, ci, si in DEFS if d_dmy(_get(row, ci))
        ]
        if recs:
            out[tag] = recs
    return out


def _retrofits(rows):
    """25 KGH Retro.csv - row 1 names, row 2 sub-headers, rows 3+ data."""
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
    SKIP = {"N/A", "NA", "--", "-", "#N/A", "NONE", ""}
    out = {}
    for row in rows[3:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = []
        for name, ci, si_col, sort in DEFS:
            raw = _get(row, ci)
            started = _get(row, si_col) if si_col is not None else ""
            date = d_dmy(raw)
            if date:
                status, dt = "complete", date
            elif raw.upper() in DONE:
                status, dt = "complete", None
            elif raw.upper() in SKIP and not started:
                if name.startswith("Generator Duct") and _get(row, 29).lower().startswith("ok"):
                    continue
                if raw.upper() in {"N/A", "NA", "--", "-", "#N/A", "NONE"}:
                    continue
                status, dt = "outstanding", None
            elif d_dmy(started) or (started and started.upper() not in SKIP):
                status, dt = "in_progress", None
            else:
                status, dt = "outstanding", None
            recs.append({"name": name, "status": status, "date": dt, "sort": sort})
        out[tag] = recs
    return out


def _components(rows):
    """Kilgallioch App data.csv - row 1 headers, row 2 sub-headers, rows 3+ data."""
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
    for row in rows[3:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        recs = []
        comm = d_dmy(_get(row, 15))
        if comm:
            recs.append({"name": "Commissioned", "date": comm, "detail": None, "sort": -1})
        for name, ci, sort in FIELDS:
            val = clean(_get(row, ci))
            if val:
                recs.append({"name": name, "date": None, "detail": val, "sort": sort})
        out[tag] = recs
    return out


def _equipment(rows):
    """Equipment info.csv - row 0 headers, rows 1+ data."""
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        out[tag] = {
            "manufacturer": clean(_get(row, 1)), "model": clean(_get(row, 2)),
            "family": clean(_get(row, 3)), "serial": clean(_get(row, 4)),
            "install_date": d_dmy(_get(row, 5)), "toc": d_dmy(_get(row, 6)),
            "warranty_expiry": d_dmy(_get(row, 7)),
        }
    return out


def _smp(rows):
    """KGH SMP Action Tracker.csv - row 1 headers, rows 2+ data."""
    out = {}
    for row in rows[2:]:
        tag = norm_tag(_get(row, 0))
        if not tag:
            continue
        out[tag] = {
            "data_date": d_mdy(_get(row, 4)),
            "gearbox": clean(_get(row, 5)), "generator": clean(_get(row, 6)),
            "main_bearing": clean(_get(row, 7)), "observations": clean(_get(row, 14)),
        }
    return out


def _manplan(rows):
    """Manplan 2025.csv - row 11 dates, rows 13-36 technicians (col 4 = name)."""
    UNAVAIL = {"HOL IN WD", "MED", "SICK", "ABS", "TRG", "PAT", "JURY"}
    date_row = rows[11] if len(rows) > 11 else []
    col_date = {}
    for i, v in enumerate(date_row):
        iso = d_dmy(v)
        if iso:
            col_date[i] = iso

    names, taken = [], set()
    roster = {}
    for row in rows[13:37]:
        name = _get(row, 4)
        if not name:
            continue
        uname = make_username(name, taken)
        names.append({"name": name, "username": uname})
        for ci, iso in col_date.items():
            code = _get(row, ci).strip()
            if code.upper() in UNAVAIL:
                roster.setdefault(iso, {})[uname] = "HOL in WD" if code.upper() == "HOL IN WD" else code.upper()
    return names, roster


def _jobreq(rows):
    """Job Request.csv (SCOTT & STUART 2026) - row 0 headers, rows 1+ data."""
    out = {}
    for row in rows[1:]:
        tag = norm_tag(_get(row, 3))
        if not tag:
            continue
        techs = [_get(row, i) for i in range(12, 18)]
        techs = [t for t in techs if t]
        wt = _get(row, 6)
        out.setdefault(tag, []).append({
            "date": d_long(_get(row, 0)),
            "description": _get(row, 8) or wt or "Work order",
            "work_type": (wt.title().replace("Hv", "HV")) or None,
            "service_order": _get(row, 11) or None,
            "technicians": ", ".join(techs) or None,
        })
    for tag in out:
        out[tag].sort(key=lambda e: e["date"] or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------

def load():
    kgh = _rows(FILES["kgh2025"])
    order, services = _turbines_and_services(kgh)
    names, roster = _manplan(_rows(FILES["manplan"]))
    return {
        "turbines": order,
        "technicians": names,
        "services": services,
        "hv": _hv(_rows(FILES["hv"])),
        "stat": _stats(_rows(FILES["stats"])),
        "retrofits": _retrofits(_rows(FILES["retro"])),
        "components": _components(_rows(FILES["components"])),
        "equipment": _equipment(_rows(FILES["equipment"])),
        "smp": _smp(_rows(FILES["smp"])),
        "roster": roster,
        "history": _jobreq(_rows(FILES["jobreq"])),
    }


if __name__ == "__main__":  # quick sanity dump
    d = load()
    print("turbines:", len(d["turbines"]), d["turbines"][:5])
    print("technicians:", len(d["technicians"]), d["technicians"][0])
    for k in ("services", "hv", "stat", "retrofits", "components", "equipment", "smp", "history"):
        print(f"{k}: {len(d[k])} turbines")
    print("roster dates:", len(d["roster"]))
    a01 = d["turbines"][0] if "A01" not in d["turbines"] else "A01"
    print("A01 hv:", d["hv"].get("A01"))
    print("A01 retro (not complete):",
          [r["name"] for r in d["retrofits"].get("A01", []) if r["status"] != "complete"])
    print("A01 smp:", d["smp"].get("A01"))
    print("A01 equipment:", d["equipment"].get("A01"))
