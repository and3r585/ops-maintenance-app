# Operations & Maintenance app

Role-based operations / maintenance tool with task planning and asset tracking.
Runs on **Python 3 standard library only** — no `pip install`, no Node, no internet.

## Run

```bash
cd "ops-app"
python3 app.py
```

Then open http://localhost:8000

```bash
python3 app.py --port 9000   # different port
python3 app.py --reset       # wipe data/ and reseed
```

Data (SQLite DB + uploaded photos) lives in `ops-app/data/`. Delete that folder to start fresh.

## Logins

| Username | Password | Role | Can |
|----------|----------|------|-----|
| `admin`  | `admin123` | Admin | Planning board + everything below |
| `sclydesdale` | `tech123` | Technician | View assets, add pending entries + photos |
| _…23 more_ | `tech123` | Technician | " |

The 24 technicians are the names in **column E, rows 14–37 of the Manplan 2025 tab**.
Usernames are first-initial + surname, lowercase (`sclydesdale`, `scant`, `jgange`, `rfrew`,
`jphillips`, `jmccartney`, `sloughran`, `jyoung`, `wmcmillan`, `lkerr`, `apatterson`,
`dhendren`, `bmullen`, `mglover`, `jzonfrillo`, `smccathie`, `slennox`, `jhunter`,
`tdempsie`, `lclark`, `lmadden`, `bsimpson`, `eplunkett`, `jmackenzie`). All use `tech123`.

## What's built

- **Login page** — username + password gate on the whole app.
- **Role landing** — technicians get "View asset information"; admins additionally get "Planning".
- **Asset register** — 96 turbines seeded from the *TURBINE* column of the *KGH 2025* tab
  of the KGH Virtual Whiteboard workbook, grouped by array (A–J). Searchable/filterable,
  with an open-pendings badge per turbine. No operational-status field on assets.
- **Technicians** — 24 names from column E, rows 14–37 of the *Manplan 2025* tab; these are
  the columns on the planning board and the assignees for jobs.
- **Site dashboard (admin)** — open pending-entry count (by status), the next 10 service due
  dates across the site (108-month + 6 months, soonest first), and every retrofit campaign
  still outstanding or in progress with the affected turbine counts.
- **Planning board (admin)** — pick a date; the left rail shows **available technicians** as
  draggable chips (unavailable ones greyed with their reason, from the *Manplan 2025* day
  grid — unavailable = `HOL in WD` / `MED` / `SICK` / `ABS` / `TRG` / `PAT` / `JURY`, blank
  or anything else = available) and the outstanding **tasks**. Drag chips and tasks into a
  **10-row team table**; each team row holds one task and its technicians, with two
  placeholder slots and a "needs 2" flag until at least two technicians are dropped in.
  Dragging a chip/task back to the rail (or its × button) frees it. The plan is saved per
  date; an unavailable technician is refused.
- **Asset detail** — tabs for **Details**, **Service dates**, **Retrofits**, **Components**,
  **History** and **Pendings**.
  - *Details* — **Equipment** card (from the **Equipment info** workbook); a **Next service
    due** card (108-month completion + 6 months, with a days-away / overdue indicator); and a
    **Condition monitoring (SMP)** box with gearbox / generator / main-bearing state and
    observations, from the *KGH SMP Action Tracker* tab.
  - *Service dates* — the seven major/minor service completion dates (72/84/90/96/102/108/114
    month), from the completion-date columns of the *KGH 2025* tab.
  - *HV history* — HV maintenance completion dates (2023 campaign, 2024/25 rephasing,
    2025/26), from the *HV* tab.
  - *Stat history* — annual and semi-annual statutory inspections plus the 10-year lift
    inspection, from the *Stats* tab.
  - *Retrofits* — every retrofit campaign with its status: completed (with date), in
    progress, or outstanding, from the *25 KGH Retro* tab (N/A rows omitted). Includes the
    transformer wall upgrade (moved here from the HV tab).
  - *Components* — nacelle/rotor/tower serial numbers, blade type, blade-bearing maker and
    commissioning date, from the *Nacelle tracability* tab of the **Kilgallioch App data**
    workbook (turbine IDs `A1`→`A01` normalised to match).
  - *History* — the work-order log (date, description, work type, service order, technicians)
    from the *SCOTT & STUART 2026* tab of the **Job Request** workbook — ~1,600 entries across
    the 96 turbines; site-wide rows (no turbine) are skipped. Filterable by work type.
    Any current scheduled/open jobs are listed above the log.
  - *Pendings* — any user can add an entry (note + up to 8 photos, camera on mobile);
    admins mark entries Submitted → Reviewed → Actioned.

All workbook data is pre-extracted into the `*.json` files below and loaded on first run —
the workbooks are not needed at runtime. Re-run `python3 app.py --reset` after changing them.
- **Planning board (admin)** — outstanding jobs are pulled into a backlog column; drag a job
  onto a technician to schedule it, drag it back to unassign. Every move is persisted and logged.

## Extending it later

Navigation is driven by the `modules` table (see `SCHEMA` in `app.py`). Add a row
(`key`, `name`, `min_role`, `sort`), add matching API routes in `_route_api`, and a view
in `web/app.js`. The two current modules (`assets`, `planning`) follow that pattern.

## Layout

```
app.py            server + schema + seed data (stdlib only)
kgh_components.json  per-turbine component serial numbers (Kilgallioch App data)
equipment_info.json  per-turbine make/model/serial/dates (Equipment info)
history_2026.json    per-turbine work-order log (Job Request, SCOTT & STUART 2026)
roster.json          technician unavailable-days by date (Manplan 2025)
kgh_services.json    per-turbine service completion dates (KGH 2025 tab)
kgh_retrofits.json   per-turbine retrofit status: complete / in progress / outstanding
hv_history.json      per-turbine HV maintenance completion dates (HV tab)
stat_history.json    per-turbine statutory inspection completion dates (Stats tab)
smp_tracker.json     per-turbine gearbox/generator/main-bearing state + observations
web/index.html    SPA shell
web/app.js        views, router, drag-and-drop planning board
web/styles.css    Claude-inspired theme (light + dark, follows OS, manual toggle ◐)
data/             created at runtime — SQLite db + uploaded photos
```
