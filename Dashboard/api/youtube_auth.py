"""One-time YouTube OAuth consent flow — run by the OWNER, once per page.

Produces the token file that youtube_upload.py reads. The browser login MUST use
the PAGE's own Google account (e.g. contentfactory.gamestory@gmail.com), NOT the
borrowed Claude account — per the per-page account-isolation rule.

Prereq (one-time):
    pip install google-api-python-client google-auth-oauthlib google-auth-httplib2

The OAuth APP credentials (client_id/client_secret) are read from the external
secrets .env (GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET) — NOT from a
client_secret.json in the repo. See oauth_env.py.

Usage (from Dashboard/api, with the venv active):
    python youtube_auth.py --page "CTG Gaming"
    # optionally override the secrets .env location:
    #   set CF_SECRETS_ENV=E:\\Installed\\ContentFactory-secrets\\.env

It opens a browser, you approve, and the token is written to:
    <repo>/Dashboard/secrets/<page-slug>/youtube.json
which matches platform_accounts.credentials_ref. After this, the runner uploads
finished videos automatically (private by default).
"""

import argparse
import json
import os
import re
import unicodedata

from oauth_env import GOOGLE_TOKEN_URI, get_oauth_app_credentials

# Upload scope only — least privilege needed to publish videos.
SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def _slug(name: str) -> str:
    """ASCII-safe slug for per-page secrets folders.

    Strips Vietnamese diacritics so the path is portable across tools and
    filesystems. Plain-ASCII names stay byte-identical to the old behavior
    (so existing secrets like ctg-gaming / gamestory are never orphaned).

    >>> _slug("Giải Thích Mọi Thứ")
    'giai-thich-moi-thu'
    >>> _slug("CTG Gaming")
    'ctg-gaming'
    >>> _slug("GameStory")
    'gamestory'
    >>> _slug("Đặng Văn Đức")
    'dang-van-duc'
    """
    # đ/Đ are NOT decomposed by NFKD — map them explicitly before normalizing.
    s = name.strip().replace("đ", "d").replace("Đ", "D")
    # NFKD splits accented chars into base + combining marks; drop the marks.
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    # Keep [a-z0-9], collapse every other run to a single dash, trim edges.
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


# Quick self-check (cheap; guards the ASCII contract on import).
assert _slug("Giải Thích Mọi Thứ") == "giai-thich-moi-thu"
assert _slug("CTG Gaming") == "ctg-gaming"
assert _slug("GameStory") == "gamestory"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the YouTube OAuth consent flow for a page.")
    ap.add_argument("--page", default="CTG Gaming", help="Page name (decides the output folder).")
    ap.add_argument("--out", default=None, help="Explicit token output path (overrides --page).")
    args = ap.parse_args()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise SystemExit(
            "Missing libraries. Run:\n"
            "  pip install google-api-python-client google-auth-oauthlib google-auth-httplib2"
        )

    # App creds come from the external secrets .env, NOT a client_secret.json in the repo.
    try:
        client_id, client_secret = get_oauth_app_credentials()
    except RuntimeError as exc:
        raise SystemExit(str(exc))

    # Build the InstalledAppFlow from an in-memory client config (no on-disk secret).
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": GOOGLE_TOKEN_URI,
            "redirect_uris": ["http://localhost"],
        }
    }

    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    out = args.out or os.path.join(repo_root, "Dashboard", "secrets", _slug(args.page), "youtube.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)

    print(f"Opening a browser to authorize the YouTube account for page '{args.page}'.")
    print("=> Log in with the PAGE's own Google account (NOT the borrowed Claude account).")
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    creds = flow.run_local_server(port=0)

    # Persist token WITHOUT the client_secret — strip it so no secret lands in the repo.
    token_data = json.loads(creds.to_json())
    token_data.pop("client_secret", None)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(token_data, f)
    print(f"\nToken written to: {out} (client_secret intentionally omitted — read from env at runtime)")
    print("The runner will now publish this page's finished videos to YouTube (private by default).")


if __name__ == "__main__":
    main()
