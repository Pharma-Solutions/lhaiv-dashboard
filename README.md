# LHAIV Discovery Dashboard — auto-refresh pipeline

Rebuilds the LighthouseAI Verified data-discovery dashboard from the SharePoint
tracker and publishes it to Netlify, with no manual export step.

```
SharePoint (Microsoft Graph)  ->  transform  ->  render HTML  ->  Netlify
```

## What's here

| File | Role |
|------|------|
| `pipeline.py` | Orchestrator. Download → transform → render → deploy. |
| `dashboard_core.py` | `extract(xlsx)` parses the tracker; `render(data, ts)` builds the HTML. |
| `_template.txt` | The validated dashboard layout (single source of visual truth). |
| `graph_client.py` | App-only Microsoft Graph download via the SharePoint share link. |
| `netlify_deploy.py` | Single-file digest deploy to Netlify (no CLI). |
| `.github/workflows/refresh.yml` | Daily scheduled run (GitHub Actions). |
| `.env.example` | The six secrets the pipeline needs. |

## Quick start (local, offline)

```bash
pip install -r requirements.txt
python pipeline.py --local /path/to/Lighthouse_API_Master_Discovery_Tracker.xlsx --no-deploy
open dist/index.html
```

This is the fastest way to confirm a tracker change renders correctly before wiring credentials.

## Full pipeline

1. Fill the six values in `.env` (copy from `.env.example`). See the header comments
   in `graph_client.py` (Azure app registration + `Sites.Read.All`) and
   `netlify_deploy.py` (Netlify site ID + personal access token).
2. Load them and run:
   ```bash
   export $(grep -v '^#' .env | xargs)
   python pipeline.py
   ```
3. The dashboard publishes to your Netlify site root.

## Scheduling

- **GitHub Actions (recommended):** push this folder to a repo, add the six values
  as repository secrets (Settings → Secrets and variables → Actions), and
  `refresh.yml` runs daily at 11:00 UTC. Trigger manually anytime from the Actions tab.
- **cron:** `0 11 * * * cd /path && export $(grep -v '^#' .env | xargs) && python pipeline.py`

## Design notes

- **Fail-safe:** the pipeline refuses to publish an empty or unparseable build —
  a bad source pull leaves the last good dashboard live rather than replacing it
  with a broken one.
- **The template is the visual contract.** Change layout, colors, or copy in
  `_template.txt`; the palette was validated for light and dark. Data shape and
  labels live in `dashboard_core.py`.
- **Not yet wired:** customer-footprint weighting. When the customer
  trading-partner list is available, join it in `extract()` and the "% of our
  customers' licenses covered" metric becomes the headline.
- This dashboard is a snapshot of *discovery status*. It is not the license
  datastore — for the multi-million-row license payloads, see the data-architecture
  recommendation (object storage for raw files + Parquet/DuckDB for analysis,
  Postgres for the product pipeline), not xlsx/csv.
