"""
Deploy a single static HTML file to Netlify via the file-digest deploy API.
No Netlify CLI required — pure REST, so it runs in CI.

Setup (one time):
  1. Create a Netlify site (netlify.com) — note its API ID (Site settings -> General
     -> Site ID), or its site name.
  2. User settings -> Applications -> Personal access token. Note the token.
The deploy publishes the file as the site's index.html (served at the site root).
"""
import hashlib
import requests

API = "https://api.netlify.com/api/v1"


def deploy_html(html_str, site_id, token, remote_path="/index.html"):
    """Digest-deploy a single file. Returns the live deploy URL."""
    # Guard (added 2026-08): fail loudly with the REAL reason instead of a bare
    # "404 Not Found for .../sites/***/deploys". Two distinct failures hide behind
    # the Netlify deploy call, and they need different fixes:
    #   * empty secret  -> NETLIFY_SITE_ID / NETLIFY_AUTH_TOKEN unset, saved as an
    #                       Environment secret while the job declares no environment,
    #                       or a name mismatch with the workflow.
    #   * 404 on deploy -> token authenticated, but no site with this ID exists UNDER
    #                       that token's account/team (wrong site id, or the token
    #                       belongs to a different account than the site). A bad token
    #                       is a 401, not a 404 — so a 404 is a site/token PAIRING problem.
    missing = [name for name, val in (("NETLIFY_SITE_ID", site_id),
                                      ("NETLIFY_AUTH_TOKEN", token))
               if not (val and str(val).strip())]
    if missing:
        raise SystemExit(
            "Netlify deploy: missing/empty secret(s): " + ", ".join(missing) + ". "
            "Set them as REPOSITORY secrets (Settings -> Secrets and variables -> Actions) "
            "and confirm the names match what the workflow references."
        )
    # tolerate a site id given as a full URL (strip scheme / trailing slash);
    # Netlify accepts either the API ID or the bare site domain (e.g. lhai-verified.netlify.app)
    site_id = str(site_id).strip().replace("https://", "").replace("http://", "").strip("/")
    token = str(token).strip()
    content = html_str.encode("utf-8")
    sha1 = hashlib.sha1(content).hexdigest()
    headers = {"Authorization": f"Bearer {token}"}

    # 1) declare the deploy and which files it contains (path -> sha1)
    create = requests.post(
        f"{API}/sites/{site_id}/deploys",
        headers={**headers, "Content-Type": "application/json"},
        json={"files": {remote_path: sha1}},
        timeout=120,
    )
    if create.status_code == 404:
        raise SystemExit(
            f"Netlify deploy: site {site_id!r} not found under this token's account "
            "(HTTP 404). The token authenticated, so this is a SITE/TOKEN PAIRING problem, "
            "not a bad token. Check that NETLIFY_SITE_ID is the correct Site ID (Netlify -> "
            "Site configuration -> General -> Site information -> Site ID) or the site's "
            "domain, AND that NETLIFY_AUTH_TOKEN belongs to the account/team that owns that "
            "site. To list what this token can see: "
            "curl -H 'Authorization: Bearer <token>' https://api.netlify.com/api/v1/sites"
        )
    if create.status_code == 401:
        raise SystemExit(
            "Netlify deploy: 401 Unauthorized — NETLIFY_AUTH_TOKEN is wrong, expired, or "
            "revoked. Generate a new personal access token (User settings -> Applications) "
            "under the account that owns the site."
        )
    if not create.ok:
        raise RuntimeError(f"Netlify deploy failed: {create.status_code} {create.text[:300]}")
    deploy = create.json()
    deploy_id = deploy["id"]

    # 2) upload the file only if Netlify says it needs it (it dedupes by sha1)
    required = deploy.get("required", [])
    if sha1 in required or not required:
        up = requests.put(
            f"{API}/deploys/{deploy_id}/files{remote_path}",
            headers={**headers, "Content-Type": "application/octet-stream"},
            data=content,
            timeout=300,
        )
        up.raise_for_status()

    return deploy.get("ssl_url") or deploy.get("deploy_ssl_url") or deploy.get("url", "")
