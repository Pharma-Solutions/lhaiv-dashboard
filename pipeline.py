#!/usr/bin/env python3
"""
LHAIV dashboard refresh pipeline.

    SharePoint (Graph)  ->  transform  ->  render HTML  ->  Netlify

Run modes:
  python pipeline.py                 # full pipeline: download from SharePoint, build, deploy
  python pipeline.py --local FILE    # build from a local .xlsx (skip SharePoint)
  python pipeline.py --no-deploy     # build only; write dist/index.html, don't deploy
  python pipeline.py --local FILE --no-deploy   # fully offline dry run

Config comes from environment variables (see .env.example). In CI, set them as
secrets. Locally, `export $(grep -v '^#' .env | xargs)` or use python-dotenv.

Fail-safe: any step that cannot be verified raises and stops the run rather than
deploying a stale or half-built dashboard. Nothing is published unless the build
succeeded end to end.
"""
import os
import sys
import argparse
import datetime

import dashboard_core


def log(msg):
    print(f"[refresh] {msg}", flush=True)


def build_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main():
    ap = argparse.ArgumentParser(description="Refresh the LHAIV discovery dashboard.")
    ap.add_argument("--local", metavar="XLSX", help="Build from a local xlsx instead of SharePoint.")
    ap.add_argument("--no-deploy", action="store_true", help="Build only; do not deploy to Netlify.")
    ap.add_argument("--out", default="dist/index.html", help="Local output path for the built HTML.")
    args = ap.parse_args()

    # 1) obtain the source workbook
    if args.local:
        xlsx = args.local
        log(f"using local workbook: {xlsx}")
    else:
        import graph_client
        tenant = os.environ["MS_TENANT_ID"]
        client = os.environ["MS_CLIENT_ID"]
        secret = os.environ["MS_CLIENT_SECRET"]
        share_url = os.environ["TRACKER_SHARE_URL"]
        xlsx = "tracker.xlsx"
        log("authenticating to Microsoft Graph (client credentials)")
        token = graph_client.get_token(tenant, client, secret)
        log("downloading tracker from SharePoint")
        graph_client.download_shared_file(share_url, token, xlsx)

    # 2) transform
    log("parsing tracker")
    data = dashboard_core.extract(xlsx)
    m = data["meta"]
    log(f"parsed {m['agencies']} agencies across {m['jurisdictions']} jurisdictions "
        f"(source verified {m['verifiedAsOf'] or 'n/a'})")
    if m["agencies"] == 0:
        raise RuntimeError("Parsed zero agencies — refusing to publish an empty dashboard.")

    # 3) render
    html = dashboard_core.render(data, build_timestamp())
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)
    log(f"built {args.out} ({round(len(html)/1024,1)} KB)")

    # 4) deploy
    if args.no_deploy:
        log("skipping deploy (--no-deploy)")
        return
    import netlify_deploy
    site = os.environ["NETLIFY_SITE_ID"]
    token = os.environ["NETLIFY_AUTH_TOKEN"]
    log("deploying to Netlify")
    live = netlify_deploy.deploy_html(html, site, token)
    log(f"published: {live}")


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        log(f"missing required environment variable: {e}. See .env.example.")
        sys.exit(2)
