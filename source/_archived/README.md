# Archived source data — one-time import only

These ten CSVs were the **initial import** for the Site Portal database. They were
parsed once by `seed_data.py` to populate `data/app.db`, and **the app no longer
reads them in normal operation.** The SQLite database is the sole source of truth;
edits made in the app are the live record.

They are kept here only for:

- **audit / provenance** — the published [data-sources map](https://claude.ai/code/artifact/e763117e-d7a1-4b71-89e6-94fb6da8bb40)
  documents which column of which file fed which field;
- **disaster recovery** — if `data/app.db` is ever lost entirely, `python3 app.py`
  against an empty database re-imports from these files.

`Credentials.csv` and `KGH_SMP.csv` (in the parent `source/` folder) are **not** archived —
they are re-synced into the database on every server start (logins, and condition-monitoring
state respectively). `KGH_SMP_Action_Tracker.csv` below is the *original* SMP import and is
superseded by `source/KGH_SMP.csv`.

| File | From | Fed |
|---|---|---|
| `KGH_2025.csv` | KGH 2025 tab | turbine register, defect notes, blade drone inspection |
| `KGH_Service_Dates.csv` | KGH Service dates_Comprehensive → "Service dates" tab | full service schedule (27 services incl. two oil exchanges) — applied once, then edited in-app |
| `HV.csv` | HV tab | HV maintenance history |
| `Stats.csv` | Stats tab | statutory + lift inspections |
| `25_KGH_Retro.csv` | 25 KGH Retro tab | 20 retrofit campaigns + status |
| `Kilgallioch_App_data.csv` | Nacelle traceability | component serials, blade config, commissioning date |
| `Equipment_info.csv` | Equipment info | manufacturer / model / family / serial / key dates |
| `KGH_SMP_Action_Tracker.csv` | SMP tracker | gearbox / generator / main-bearing condition |
| `Manplan.csv` | Manplan 2025 | technician availability roster |
| `Job_Request.csv` | Scott & Stuart 2026 | ~1,600 work-order history records |
| `Pendings.csv` | SGRE/SAP export | ~570 pending notifications |

**Do not run `python3 app.py --reset`** on a live database — it deletes `data/app.db`
and re-imports from these files, discarding every app-entered pending, photo and edit.
