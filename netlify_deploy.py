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
    create.raise_for_status()
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
