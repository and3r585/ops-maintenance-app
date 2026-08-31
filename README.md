# Site Portal

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

Data (SQLite DB + uploaded photos) lives in `data/` (or `$DATA_DIR`). Delete it to start fresh.
The server honours `$PORT` and `$HOST` when set (binds `0.0.0.0` on a host).

## Deploy (Render)

This is a **persistent Python process** with a local SQLite database — it will **not** run on
serverless platforms like Vercel/Netlify (no long-running process, ephemeral filesystem).
Use a host that runs a real process:

1. In the [Render](https://render.com) dashboard: **New → Blueprint** and pick this repo.
2. Render reads [`render.yaml`](render.yaml), builds (nothing to install) and starts `python app.py`.
3. Log in with `admin` and the generated `ADMIN_PASSWORD` (Environment tab of the service).

The default is Render's **free** plan: fully working, but the disk is wiped on every restart,
so pending entries / photos added *through the app* don't survive a restart (the ~570 imported
pendings and everything else re-seed from `source/*.csv` on each cold start). For durable
storage, uncomment the paid `plan` + `disk` + `DATA_DIR` block in `render.yaml`.

Railway / Fly.io / a small VPS work the same way — run `python app.py`, give it a volume for
`$DATA_DIR`.

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
- **Asset detail** — tabs for **Details**, **Service dates**, **HV history**, **Stat history**,
  **Retrofits**, **Components**, **History** and **Pendings**.
  - *Details* — **Identification** (turbine / type / location); an **Equipment** card;
    a **Defect / operational issue** card (KGH 2025 column G — highlighted when present);
    a **Next service due** card (108-month completion + 6 months, with a days-away / overdue
    indicator); and a **Condition monitoring (SMP)** box with gearbox / generator /
    main-bearing state and observations.
  - *Service dates* — the seven major/minor service completion dates (72/84/90/96/102/108/114
    month), from `KGH_2025.csv`.
  - *HV history* — HV maintenance completion dates (2023 campaign, 2024/25 rephasing,
    2025/26), from `HV.csv`.
  - *Stat history* — annual and semi-annual statutory inspections plus the 10-year lift
    inspection, from `Stats.csv`.
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
  - *Pendings* — ~570 open SGRE/SAP notifications imported from `Pendings.csv` (each with its
    WO number, priority and affected system; `CREA`→Submitted, `APR`→Reviewed). Any user can
    also add an entry (note + up to 8 photos); admins move entries Submitted → Reviewed →
    Actioned.

## Data

All content is seeded from **CSV exports of the source spreadsheets**, one file per tab, in
`source/`. `seed_data.py` parses them on first run (`--reset` to re-seed after editing a CSV);
nothing is needed at runtime once the DB is built. Dates in any of `YYYY-MM-DD`,
`DD/MM/YYYY` or `Weekday, D Month YYYY` form are all accepted.

| `source/` file | feeds |
|---|---|
| `KGH_2025.csv` | turbine list, service completion dates (72–114 month), the per-turbine defect note (col G) |
| `HV.csv` | HV maintenance history |
| `Stats.csv` | statutory inspection history |
| `25_KGH_Retro.csv` | retrofit status (complete / in progress / outstanding) |
| `Kilgallioch_App_data.csv` | component serial numbers (blade-bearing columns are blank in the export) |
| `Equipment_info.csv` | make / model / family / serial / dates |
| `KGH_SMP_Action_Tracker.csv` | gearbox / generator / main-bearing state + observations |
| `Manplan.csv` | technicians + the day-by-day availability roster |
| `Job_Request.csv` | per-turbine work-order log |
| `Pendings.csv` | open pending notifications per turbine |

## Extending it later

Navigation is driven by the `modules` table (see `SCHEMA` in `app.py`). Add a row
(`key`, `name`, `min_role`, `sort`), add matching API routes in `_route_api`, and a view
in `web/app.js`.

## Layout

```
app.py            server + schema + seed orchestration (stdlib only)
seed_data.py      CSV parsers -> structures the seeder consumes
source/*.csv      one CSV per source-spreadsheet tab
render.yaml       Render deploy blueprint
requirements.txt  empty (marks the repo as Python for Render)
web/index.html    SPA shell
web/app.js        views, router, planning board
web/styles.css    Claude-inspired theme (light + dark, follows OS, manual toggle ◐)
data/             created at runtime — SQLite db + uploaded photos
```
