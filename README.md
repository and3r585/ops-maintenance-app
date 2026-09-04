# Site Portal

Role-based operations / maintenance tool with task planning and asset tracking.
Runs on the **Python 3 standard library**, plus **Pillow** for resizing pending-entry
photos. No Node, no build step. (Without Pillow the app still runs and stores photos as-is.)

## Run

```bash
cd "ops-app"
pip install -r requirements.txt   # Pillow
python3 app.py
```

Then open http://localhost:8000

```bash
python3 app.py --port 9000        # different port
python3 app.py --reset --force    # DESTRUCTIVE: wipe data/app.db and rebuild from source/_archived/*.csv
```

### The database is the source of truth

The initial import from the spreadsheet CSVs has been done. `data/app.db` (or
`$DATA_DIR/app.db`) is now the **only** live datastore — the app reads nothing from the
CSVs in normal operation. Everything entered through the app (pending entries, completion
photos/comments, dates edited in Asset Information or the Data Explorer) is written straight
to it and **persists across restarts**: plain `python3 app.py` keeps it, and the seed step
only touches tables that are completely empty.

The ten import CSVs now live in [`source/_archived/`](source/_archived/) and are read **only**
to rebuild a fresh, empty database. `source/Credentials.csv` is *not* archived — it stays as
the live login list, re-synced into the `users` table on every start.

The **only** things that wipe `data/app.db`:
- `python3 app.py --reset --force` — refuses without `--force` once the DB holds any
  app-entered data (photos, edits, day plans, technician-logged pendings)
- deleting the file yourself
- a host with an **ephemeral filesystem** (see Render note below)

The startup banner prints the DB path, whether it was kept or freshly seeded, and how many
pending entries / logged edits it holds — check it if you think something didn't save.

The server honours `$PORT` and `$HOST` when set (binds `0.0.0.0` on a host).

## Deploy (Render)

This is a **persistent Python process** with a local SQLite database — it will **not** run on
serverless platforms like Vercel/Netlify (no long-running process, ephemeral filesystem).
Use a host that runs a real process:

1. In the [Render](https://render.com) dashboard: **New → Blueprint** and pick this repo.
2. Render reads [`render.yaml`](render.yaml), runs `pip install -r requirements.txt` and starts `python app.py`.
3. Log in with `admin` and the generated `ADMIN_PASSWORD` (Environment tab of the service).

[`render.yaml`](render.yaml) is configured for **durable storage**: the `starter` plan with a
1 GB persistent disk mounted at `/var/data`, and `DATA_DIR=/var/data` so `app.db` and uploaded
photos live on that disk. Pending entries, completion evidence and in-app date edits **survive
restarts and deploys**. The ~570 imported pendings seed once from `source/*.csv` on the first
boot and are then left alone.

Cost: `starter` is ~$7/mo plus ~$0.25/GB-mo for the disk. A service with a disk runs a single
instance and skips Render's zero-downtime deploys (a few seconds' blip per deploy).

To run free instead (and lose all app-entered data on every restart/deploy): set `plan: free`
in `render.yaml` and remove the `disk:` block and the `DATA_DIR` env var.

Railway / Fly.io / a small VPS work the same way — run `python app.py`, give it a volume for
`$DATA_DIR`.

## Logins

Every account comes from **`source/Credentials.csv`** (`Name, First name, Username, Access,
Password`). `Access` maps to a role: **`Admin`** → Admin, **`View`** → View,
**`Contractor`** → Contractor, anything else → Technician. The list is re-synced into the
database on **every server start** — edit the CSV, restart, and the usernames / passwords /
roles update in place (accounts dropped from the CSV are deactivated, not deleted, so
history stays intact). A built-in `admin` / `admin123` break-glass account is always kept
(override with `$ADMIN_PASSWORD`).

| Role | Sees | Can change |
|------|------|-----------|
| **Contractor** | Site Dashboard, Asset Information | **only** add a pending entry (note + photo, same as a technician) |
| **Technician** | Site Dashboard, Asset Information, their own Technician Roster | **only** add a pending entry, or complete a reviewed one (mandatory comment + photo) |
| **View** | everything an Admin sees — Site Dashboard, Asset Information, Technician Roster (all), Notification Request, Data Explorer | **nothing** — every page is read-only; CSV / change-report exports still work |
| **Admin** | everything | everything — service/HV/retrofit/blade dates, pending review + parts, notification requests, bulk edits |

Every `Contractor` login also gets a `roster_tech` row (synced each start) so contractors
appear in **Notification Request** — in their own box between *Available technicians* and
*Unavailable*, always available — but never on the Technician Roster calendar.

## What's built

- **Login page** — username + password gate on the whole app.
- **Role landing** — technicians get "Site dashboard", "View asset information" and their
  own "Technician Roster"; admins additionally get "Notification Request" and "Data Explorer".
- **Asset register** — 96 turbines seeded from the *TURBINE* column of the *KGH 2025* tab
  of the KGH Virtual Whiteboard workbook, grouped by array (A–J). Searchable/filterable,
  with an open-pendings badge per turbine. No operational-status field on assets.
- **Technician roster** — `roster_tech` (the master technician list) + `roster_day` (one
  Manplan Key code per technician per day; blank = no row). Seeded once from the *Manplan*
  tab for the technicians that were in Notification Request, then **app-owned** — the Excel
  can be retired. `roster_tech` is now the master list for Notification Request: add or
  archive a technician on the roster's **Manage technicians** panel and it mirrors into
  Notification Request. Archiving keeps the calendar; the technician just leaves the active
  list. (The old sparse `roster` table was dropped in the `roster_calendar_v1` migration.)
- **Site dashboard** — open pending-entry count (by status), each turbine's next incomplete
  service (soonest first), and every retrofit campaign still outstanding / in progress.
  Visible to everyone. Its three drill-downs — **`#/pendings`**, **`#/services`**,
  **`#/retrofits`** — let an **admin edit completion dates in place**: on the *Service
  completions* list, adding a date to a turbine's next service records it and the row
  advances to the following service; a **Start date** can also be set there on any
  102-month-and-up service, the same as on the asset's Service dates tab. The *Retrofit
  completions* list groups items by campaign — each campaign collapses/expands (with an
  **Expand all** toggle) and adding a date to a turbine closes that campaign out for it.
  Every edit goes through the approval modal, writes to `asset_records` via `PATCH
  /api/records/:id`, and shows up everywhere that reads it (the asset's own tabs, the
  Data Explorer, the change log).
  - *Next service due* (dashboard + the asset Details card + `#/services`) = the first
    service with no completion date that falls after the last completed one; its **planned**
    date is the last completion + the interval between the two (72→84 is +12 months, the
    rest +6), or the install date + months when nothing is done yet. If a start date has
    been set on it, the due date reads **"Started · &lt;date&gt;"**.
- **Data Explorer (admin only)** — two tools in one page:
  - *Bulk table* — pick any asset tab (Service dates, HV history, Stat history, Retrofits,
    Blades, Components) and get one table with a row per turbine and a column per record.
    Cells are editable inline; edits are staged and highlighted, then a **Review & save**
    step shows every change as `was → new` before it is written to SQLite. Each save is
    written to a `record_changes` audit log. **Export this table (CSV)** dumps the current
    table as-is. **A cell that is blank in the database stays blank here** — no `—`, no
    placeholder text. Two read-only flat tabs are also listed here, each with its own CSV
    export: **Roster notes** (Date · Note · Entered by) and **Service start dates**
    (Turbine · Service · Start date · Completed — every service that has a planned start,
    kept even after it's completed).
  - *Completions report* — pick a from/to date and download an **.xlsx workbook** with a
    worksheet for **every** asset tab (Service dates, HV, Stat, Retrofits, Blades,
    Components) listing every record whose completion date falls in that window — whether
    the date was imported from the spreadsheets or entered in the app — plus Pendings,
    Roster notes and Service start dates sheets for the window.
- **Technician Roster** — a month calendar, visible to everyone. `TECHNICIAN` logins see
  **only their own** calendar (read-only); `VIEW` sees everyone (read-only); `ADMIN` edits.
  ‹ › page months. A single technician → a Mon–Sun calendar with a Manplan-Key dropdown per
  day (blank / `KILG` / `CARS` / `BC` / `HOL in WD` / `HOL Apprvd` / `TRG` / `MED` / `SICK` /
  `ABS` / `COVID` / `SD` / `ROST ON` / `ON CALL` / `OFF` / `PAT` / `JURY`) and a bottom
  banner: **sick days** (`SICK`) and **holidays used** (`HOL in WD` + `HOL Apprvd`) for the
  displayed month's calendar year. **Team** shows every technician — as a calendar (each day
  lists who's on what) or a **grid** (technicians × days, colour-coded); clicking a day
  number in the grid highlights everyone **available** that day (blank / `KILG`) in green.
  On the team calendar, any day can carry a free-text **note** (☆ / ★ marker → view / edit /
  delete); notes are logged with date + author and show as a **Roster notes** tab in the
  Data Explorer (with CSV export and a sheet in the completions report). A **TRG** (training)
  day gets its own per-technician training note — click the `TRG` chip/cell in any view
  (team calendar, grid, or individual); an admin, or the technician on their own day, can
  edit it, everyone else reads it. Admins get **Manage
  technicians** to add (name + optional login link), archive (calendar kept, leaves
  Notification Request) and restore.
- **Notification Request (admin)** — pick a **roster date**; the left rail shows **available
  technicians** as draggable chips, straight from the roster: available only on a **blank or
  `KILG`** day, every other code greyed with the code as the reason. A **Contractors** box
  sits between available and unavailable — every `Contractor` login, always available. Drag
  technicians (or contractors) into **teams of up to 4**; a request needs a **contract type** (dropdown mirrored from the notification-request
  workbook), a **description**, at least one technician, and a **turbine**. Six contract
  types are the exception — `STORES - SERVICE`, `STORES - CORRECTIVE`, `SUPERVISOR DUTIES`,
  `VEHICLE CHECK`, `WEATHER/STAND DOWN`, `GENERAL ADMIN` — they hide the turbine field, are
  export-only and are filed nowhere. Every other (turbine) request is saved to that turbine's
  history on submit. An optional **ATS Case** (appended to the history description) and an
  optional **SON** (Service Order Number → the *SGRE Service Order Number* column and the
  history `service_order`) sit alongside the description. A fresh empty team appears once
  the previous one has a technician, and stops when no available technicians remain. A
  technician placed on more than one team that date raises a duplicate **warning** (not a
  block). **Submit Request** (enabled once any one request is complete) downloads the
  `.xlsx` — laid out for copy/paste into the target sheet (Hub `SO5`, Site `Kilgallioch`,
  turbine `<tag> Kilgallioch` or blank for the six exceptions, six technician columns) —
  files every turbine request into that turbine's **History** tab (tagged *Notification
  request*), and clears the whole board (incomplete drafts included) for the next set.
  Submit also queues every request in the **outbox** (`notif_outbox`) for delivery straight
  into the shared company workbook — decoupled from submit itself, so a slow or unreachable
  workbook never blocks or fails a submission. A **Recent submissions** panel below the board
  shows each submission's delivery status (pending / delivered / failed, per request), lets
  an admin **download** that submission's `.xlsx`, **retry** a failed delivery, or **mark it
  done by hand** once it's been pasted in manually. Delivery itself is a background worker
  that POSTs queued rows as JSON to `$NOTIF_OUTBOX_WEBHOOK` (e.g. a Power Automate flow doing
  *Add a row into a table*), with `$NOTIF_OUTBOX_API_KEY` sent as `X-Api-Key`; unset, rows
  just queue for manual review — no code change needed to turn delivery on later.
- **Asset detail** — tabs for **Details**, **Service dates**, **HV history**, **Stat history**,
  **Retrofits**, **Blades**, **Components**, **History** and **Pendings**, with **‹ ›**
  previous/next-turbine buttons in the header that step through the register alphabetically
  by tag (wrapping at both ends) while staying on whichever tab you're viewing.
  Technicians see every tab **read-only** except Pendings (add / complete). The date
  editors below (**＋ Add date**) show for admins only; for a technician a missing date is
  simply blank.
  - *Details* — **Identification** (turbine / type / location); an **Asset Details** card
    (make / model / family / serial / key dates, from `Equipment_info.csv`);
    a **Defect / operational issue** card — free text, **admin-editable** (View / technician
    read-only), highlighted when present; seeded once from KGH 2025 column G, then owned
    entirely by the database;
    a **Next service due** card (the next incomplete service, its planned date, and
    "Started · &lt;date&gt;" if a start date is on it — see the
    dashboard note above — with a days-away / overdue indicator); and a **Condition
    monitoring (SMP)** box with gearbox / generator / main-bearing state and observations,
    re-synced from `source/KGH_SMP.csv` on every start (states: Normal / Monitoring /
    Action&nbsp;-&nbsp;Low/Medium/High / Damaged / No&nbsp;SMP&nbsp;data).
  - *Service dates* — a **Service schedule** table (72 → 132 month) with:
    **Planned** = the previous service's completion date plus the interval (derived — so
    entering a completion date advances the next service's planned date, e.g. 108-month
    completed → 114-month planned); an optional **Start date** admins can set on each
    still-open service from **102 months on** (it disappears once a completion date is
    entered); and **Completed** = the editable date (`PATCH /api/records/:id`). 72–114
    seed from `KGH_2025.csv`; 120/126/132 start blank. The **5-Year Oil Exchange** sits in
    its own box below, tracked separately.
    Any date an **admin** changes on an asset opens an **approval modal** (was → new)
    before it is written and logged — the same review step as the Data Explorer.
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
  - *Pendings* — ~570 open SGRE/SAP notifications imported from `Pendings.csv` (priority and
    affected system; `CREA`→Submitted, `APR`→Reviewed). Flow is
    **Submitted → Reviewed → Completed**. Raising a new entry needs a **note + at least one
    photo**. To move an entry to **Reviewed** an admin must assign a **priority of 1–5**
    (1 highest); the admin may then reserve parts (part numbers, quantities, service order).
    While **Reviewed**, *either* an admin or a technician can **Add photo / part** — an
    optional photo plus an optional part number + quantity, each stamped with who added it
    and when; the entry records its last-changed-by/at. A **technician** completes with a
    mandatory comment + closure photo; an **admin** may close without a photo after
    confirming a "close without a photo?" warning box. Completing plays a short chime.
    Re-opening a completed entry (→ Reviewed / Submitted) clears its completion note and
    evidence photos. On **app entry** an admin gets a one-off pop-up if any entries are
    still awaiting review, and an **orange dot** stays on the *Site Dashboard* nav item,
    the *Open pending entries* KPI and the *Submitted* filter chip until the queue is clear.
    The **#/pendings** list (from the dashboard) filters by status and its **Export (CSV)**
    button exports exactly the current filter.
    Photos are resized to 4000&nbsp;px / q90 on upload (Pillow) and get a 480&nbsp;px list
    thumbnail; the list shows the thumbnail, clicking opens the full image. Files live in
    `data/uploads/` and are served with a one-year immutable cache. Every photo URL is built
    by `photo_url()` / `photo_thumb_url()` in `app.py` — the single seam for moving the store
    to object storage (e.g. Cloudflare R2) later.

## Data

The database was seeded from these CSVs once. `data/app.db` is now authoritative and the
files below are **archived in [`source/_archived/`](source/_archived/)** — `seed_data.load_data()`
reads them only when `data/app.db` is empty (a from-scratch rebuild). Every seeded row still
traces to one of them; the [data-sources map](https://claude.ai/code/artifact/e763117e-d7a1-4b71-89e6-94fb6da8bb40)
documents the column-by-column mapping. Dates in any of `YYYY-MM-DD`, `DD/MM/YYYY` or
`Weekday, D Month YYYY` form are accepted.

| archived file | fed |
|---|---|
| `KGH_2025.csv` | turbine list, service completion dates (72–114 month), the per-turbine defect note (col G) |
| `HV.csv` | HV maintenance history |
| `Stats.csv` | statutory inspection history |
| `25_KGH_Retro.csv` | retrofit status (complete / in progress / outstanding) |
| `Kilgallioch_App_data.csv` | component serial numbers (blade-bearing columns are blank in the export) |
| `Equipment_info.csv` | make / model / family / serial / dates |
| `Manplan.csv` | the day-by-day technician availability roster |
| `Job_Request.csv` | per-turbine work-order log |
| `Pendings.csv` | open pending notifications per turbine |

Two files in `source/` are **not** archived — they are re-synced into the database on every
server start:

- **`Credentials.csv`** — login accounts (username, password, Admin / View / Tech) → `users`.
- **`KGH_SMP.csv`** — condition-monitoring state (`Turbine, Data date, State Gearbox, State
  Generator, State Main Bearing, Observations`) → the `smp_*` columns on `assets`. The old
  values are **discarded and replaced wholesale** each start; a turbine absent from the file
  is left blank. Drop in a fresh monthly SMP export (converted to this column layout),
  commit, redeploy. The original `KGH SMP May 26` export is the current source.

## Extending it later

Navigation is driven by the `modules` table (see `SCHEMA` in `app.py`). Add a row
(`key`, `name`, `min_role`, `sort`), add matching API routes in `_route_api`, and a view
in `web/app.js`.

## Layout

```
app.py               server + schema + seed orchestration + photo handling
seed_data.py         CSV parsers (load_credentials every boot; load_data only to rebuild)
source/Credentials.csv   live login list, re-synced each start
source/_archived/*.csv   the one-time import — read only to rebuild an empty DB
render.yaml          Render deploy blueprint
requirements.txt     Pillow
web/index.html       SPA shell
web/app.js           views, router, notification request
web/styles.css       Claude-inspired theme (light + dark, follows OS, manual toggle ◐)
data/                created at runtime — SQLite db + uploaded photos (+ _thumb.jpg)
```
