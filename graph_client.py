"""
Download a file from SharePoint / OneDrive via Microsoft Graph using app-only
(client-credentials) auth. No interactive login, so it runs unattended in cron/CI.

Setup (one time, in Azure Entra ID):
  1. Register an application. Note the Directory (tenant) ID and Application (client) ID.
  2. Certificates & secrets -> new client secret. Note the value.
  3. API permissions -> Microsoft Graph -> Application permissions -> Sites.Read.All
     (or Files.Read.All) -> Grant admin consent.
The share URL is the SharePoint "Copy link" URL for the tracker file — the same
one already in use. Graph resolves it via the /shares endpoint, so you do not need
to hunt down site and drive IDs.
"""
import base64
import requests

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"


def get_token(tenant_id, client_id, client_secret):
    resp = requests.post(
        TOKEN_URL.format(tenant=tenant_id),
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": "https://graph.microsoft.com/.default",
            "grant_type": "client_credentials",
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _encode_share_url(share_url):
    # Graph share-id encoding: base64url of the URL, "u!" prefix, no padding.
    b64 = base64.urlsafe_b64encode(share_url.encode("utf-8")).decode("ascii").rstrip("=")
    return "u!" + b64


def download_shared_file(share_url, token, out_path):
    """Download the driveItem behind a SharePoint sharing link to out_path."""
    headers = {"Authorization": f"Bearer {token}"}
    share_id = _encode_share_url(share_url)
    url = f"{GRAPH}/shares/{share_id}/driveItem/content"
    resp = requests.get(url, headers=headers, allow_redirects=True, timeout=300)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)
    return out_path
