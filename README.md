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
python3 app.py --reset       # DESTRUCTIVE: delete data/ and re-seed from source/*.csv
```

### Where your data lives

Everything entered *through the app* — pending entries, completion photos/comments, dates
added or edited in Asset Information or the Data Explorer — is written to the SQLite file
`data/app.db` (or `$DATA_DIR/app.db`). It **persists across restarts**: plain `python3 app.py`
keeps it, and re-seeding only fills tables that are still empty, so your entries are never
touched.

The **only** things that wipe it:
- `python3 app.py --reset` (deletes `data/` on purpose)
- deleting `data/` yourself
- a host with an **ephemeral filesystem** (see Render note below)

The startup banner prints the DB path, whether it was kept or freshly seeded, and how many
pending entries / logged edits it holds — check it if you think something didn't save.

The server honours `$PORT` and `$HOST` when set (binds `0.0.0.0` on a host).

## Deploy (Render)

This is a **persistent Python process** with a local SQLite database — it will **not** run on
serverless platforms like Vercel/Netlify (no long-running process, ephemeral filesystem).
Use a host that runs a real process:

1. In the [Render](https://render.com) dashboard: **New → Blueprint** and pick this repo.
2. Render reads [`render.yaml`](render.yaml), builds (nothing to install) and starts `python app.py`.
3. Log in with `admin` and the generated `ADMIN_PASSWORD` (Environment tab of the service).

⚠️ The default is Render's **free** plan, which has an **ephemeral filesystem**: the whole
container (including `data/app.db`) is wiped on every restart *and every deploy*. With
`autoDeploy: true`, each push to `main` redeploys and wipes it — so a pending a technician
adds today is gone the next time the service restarts. The ~570 imported pendings and all
other seed data re-appear from `source/*.csv` on each cold start (~1s), but **app-entered
data does not**.

To keep app-entered data on Render, switch to a paid plan with a persistent disk: uncomment
the `plan: starter` + `disk:` + `DATA_DIR` block at the bottom of [`render.yaml`](render.yaml)
and redeploy. That mounts a real volume at `/var/data` and points `$DATA_DIR` at it, so
`app.db` and uploaded photos survive restarts and deploys.

Railway / Fly.io / a small VPS work the same way — run `python app.py`, give it a volume for
`$DATA_DIR`.

## Logins

Every account comes from **`source/Credentials.csv`** (`Name, First name, Username, Access,
Password`). `Access = Admin` → **Admin**, anything else → **Technician**. The list is
re-synced into the database on **every server start** — edit the CSV, restart, and the
usernames / passwords / roles update in place (accounts dropped from the CSV are
deactivated, not deleted, so history stays intact). A built-in `admin` / `admin123`
break-glass account is always kept (override with `$ADMIN_PASSWORD`).

| Role | Sees | Can change |
|------|------|-----------|
| **Technician** | Site Dashboard, Asset Information | **only** add a pending entry, or complete a reviewed one (mandatory comment + photo) |
| **Admin** | everything, incl. Planning + Data Explorer | everything — service/HV/retrofit/blade dates, pending review + parts, planning, bulk edits |

## What's built

- **Login page** — username + password gate on the whole app.
- **Role landing** — technicians get "Site dashboard" + "View asset information"; admins
  additionally get "Planning" and "Data Explorer".
- **Asset register** — 96 turbines seeded from the *TURBINE* column of the *KGH 2025* tab
  of the KGH Virtual Whiteboard workbook, grouped by array (A–J). Searchable/filterable,
  with an open-pendings badge per turbine. No operational-status field on assets.
- **Technicians / roster** — the day-by-day availability grid comes from column E, rows
  14–37 of the *Manplan 2025* tab; a roster row attaches to a login account when the
  Manplan-derived username matches a `Credentials.csv` username. The planning-board columns
  and job assignees are the **active technician accounts** from `Credentials.csv`.
- **Site dashboard** — open pending-entry count (by status), the next 10 service due
  dates across the site (108-month + 6 months, soonest first), and every retrofit campaign
  still outstanding or in progress with the affected turbine counts. Visible to everyone;
  read-only.
- **Data Explorer (admin only)** — two tools in one page:
  - *Bulk table* — pick any asset tab (Service dates, HV history, Stat history, Retrofits,
    Blades, Components) and get one table with a row per turbine and a column per record.
    Cells are editable inline; edits are staged and highlighted, then a **Review & save**
    step shows every change as `was → new` before it is written to SQLite. Each save is
    written to a `record_changes` audit log. **Export this table (CSV)** dumps the current
    table as-is. **A cell that is blank in the database stays blank here** — no `—`, no
    placeholder text.
  - *Change report* — pick a from/to date and download an **.xlsx workbook** with one
    worksheet per asset tab that had any edit in that window (plus a Pendings sheet for
    pending entries raised/reserved/completed in the window). Only tabs that actually
    changed get a sheet. Edits made on an asset's own tab are captured here too, not just
    Data Explorer ones.
- **Planning board (admin)** — pick a date; the left rail shows **available technicians** as
  draggable chips (unavailable ones greyed with their reason, from the *Manplan 2025* day
  grid — unavailable = `HOL in WD` / `MED` / `SICK` / `ABS` / `TRG` / `PAT` / `JURY`, blank
  or anything else = available) and the outstanding **tasks**. Drag chips and tasks into a
  **10-row team table**; each team row holds one task and its technicians, with two
  placeholder slots and a "needs 2" flag until at least two technicians are dropped in.
  Dragging a chip/task back to the rail (or its × button) frees it. The plan is saved per
  date; an unavailable technician is refused.
- **Asset detail** — tabs for **Details**, **Service dates**, **HV history**, **Stat history**,
  **Retrofits**, **Blades**, **Components**, **History** and **Pendings**, with **‹ ›**
  previous/next-turbine buttons in the header that step through the register alphabetically
  by tag (wrapping at both ends) while staying on whichever tab you're viewing.
  Technicians see every tab **read-only** except Pendings (add / complete). The date
  editors below (**＋ Add date**) show for admins only; for a technician a missing date is
  simply blank.
  - *Details* — **Identification** (turbine / type / location); an **Equipment** card;
    a **Defect / operational issue** card (KGH 2025 column G — highlighted when present);
    a **Next service due** card (108-month completion + 6 months, with a days-away / overdue
    indicator); and a **Condition monitoring (SMP)** box with gearbox / generator /
    main-bearing state and observations.
  - *Service dates* — service completion dates 72 → 132 month (72/84/90/96/102/108/114/120/126/132)
    plus the **5-Year Oil Exchange**. 72–114 and the oil exchange seed from `KGH_2025.csv`;
    120/126/132 have no source column yet. Any missing date shows a **＋ Add date** button
    that writes straight to SQLite (`PATCH /api/records/:id`).
  - *Blades* — blade drone inspection date (from `KGH_2025.csv`) with the same **＋ Add date**
    editor, and a read-only blade-configuration summary (type + serials).
  - *HV history* — HV maintenance completion dates for the 2023 campaign, 2024/25 rephasing,
    2025/26, and now **2026/27** and **2027/28**, from `HV.csv`. The two future campaigns
    have no source column yet, so they seed blank with a **＋ Add date** editor, same as
    Service dates.
  - *Stat history* — annual and semi-annual statutory inspections plus the 10-year lift
    inspection, from `Stats.csv` (read-only).
  - *Retrofits* — every retrofit campaign, from the *25 KGH Retro* tab (N/A rows omitted).
    Includes the transformer wall upgrade (moved here from the HV tab). Completed campaigns
    show their date; outstanding/in-progress ones carry a status badge plus a **＋ Add date**
    editor — entering a date pushes it to SQLite and flips the record to complete, clearing
    it reverts to outstanding.
  - *Components* — nacelle/rotor/tower serial numbers, blade type, blade-bearing maker and
    commissioning date, from the *Nacelle tracability* tab of the **Kilgallioch App data**
    workbook (turbine IDs `A1`→`A01` normalised to match).
  - *History* — the work-order log (date, description, work type, service order, technicians)
    from the *SCOTT & STUART 2026* tab of the **Job Request** workbook — ~1,600 entries across
    the 96 turbines; site-wide rows (no turbine) are skipped. Filterable by work type.
    Any current scheduled/open jobs are listed above the log.
  - *Pendings* — ~570 open SGRE/SAP notifications imported from `Pendings.csv` (each with its
    WO number, priority and affected system; `CREA`→Submitted, `APR`→Reviewed). Flow is
    **Submitted → Reviewed → Completed**: an admin reviews (and may reserve parts — part
    numbers, quantities, service order); a technician completes with a mandatory comment +
    evidence photo. The **#/pendings** list (from the dashboard) filters by status and its
    **Export (CSV)** button exports exactly the current filter.

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
| `Manplan.csv` | the day-by-day technician availability roster |
| `Job_Request.csv` | per-turbine work-order log |
| `Pendings.csv` | open pending notifications per turbine |
| `Credentials.csv` | login accounts — username, password, Admin/Tech (synced on every start) |

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
