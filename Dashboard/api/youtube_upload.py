"""YouTube upload — the final publish step. Lazy, best-effort, owner-gated.

Publishing requires the page's OWN Google account (the per-page isolation rule)
and an OAuth token. Neither the Google client libraries nor the token live in
the repo, so this module imports lazily and returns a 'not configured' status
instead of crashing when they are absent — the local pipeline still produces the
finished MP4 either way.

To enable real uploads the OWNER must (borrowed-account caveat: this is the
owner's Google project, never the logged-in Claude account):
  1. pip install google-api-python-client google-auth-oauthlib google-auth-httplib2
  2. create an OAuth "desktop app" client in their own Google Cloud project and
     enable the YouTube Data API v3
  3. run the one-time consent flow to produce the token JSON referenced by
     platform_accounts.credentials_ref (e.g. Dashboard/secrets/<page>/youtube.json)

Uploads default to PRIVATE so a brand-new channel isn't flooded with unreviewed
videos — the owner flips them to public after checking.
"""

import json
import os

from oauth_env import GOOGLE_TOKEN_URI, get_oauth_app_credentials

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


def _build_credentials(token_path: str):
    """Build a google.oauth2 Credentials from a per-page token file + env app creds.

    The per-page youtube.json holds the user's token/refresh_token/scopes but NO
    client_secret (removed for security). The OAuth app client_id/client_secret come
    from the external secrets .env via os.environ. Combining them lets the library
    refresh the access token without any client_secret living in the repo.
    """
    from google.oauth2.credentials import Credentials

    with open(token_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    client_id, client_secret = get_oauth_app_credentials()  # raises clear error if unset
    return Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri") or GOOGLE_TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=data.get("scopes") or [YOUTUBE_UPLOAD_SCOPE],
    )


def upload_video(
    token_path: str | None,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = "20",   # 20 = Gaming
) -> dict:
    """Upload one MP4. Returns {ok, videoId?, url?, reason?}.

    Never raises for the expected "not set up yet" cases — those come back as
    {"ok": False, "reason": ...} so the runner can leave the video as 'ready'
    rather than failing the whole job.
    """
    if not token_path:
        return {"ok": False, "reason": "no credentials_ref for this page's YouTube account"}
    if not os.path.isfile(token_path):
        return {"ok": False, "reason": f"OAuth token not found at {token_path} (run the consent flow)"}
    if not os.path.isfile(video_path):
        return {"ok": False, "reason": f"video file not found: {video_path}"}

    try:
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        return {
            "ok": False,
            "reason": "google client libraries not installed "
            "(pip install google-api-python-client google-auth-oauthlib google-auth-httplib2)",
        }

    try:
        creds = _build_credentials(token_path)  # app creds from env + token from file
        if not creds.valid and creds.refresh_token:
            creds.refresh(Request())
        youtube = build("youtube", "v3", credentials=creds)
        body = {
            "snippet": {
                "title": title[:100] or "video",
                "description": description[:4900],
                "tags": tags or [],
                "categoryId": category_id,
            },
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        media = MediaFileUpload(video_path, chunksize=-1, resumable=True, mimetype="video/mp4")
        request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        vid = response["id"]
        return {"ok": True, "videoId": vid, "url": f"https://youtu.be/{vid}", "privacy": privacy}
    except Exception as exc:  # network/quota/auth errors — report, don't crash the job
        return {"ok": False, "reason": f"upload failed: {exc}"}
