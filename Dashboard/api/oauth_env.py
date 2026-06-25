"""Google OAuth APP credentials, loaded from an .env file OUTSIDE the repo.

Security: the OAuth app client_id/client_secret must NOT live in the repo tree.
They are read from an external .env (default: E:\\Installed\\ContentFactory-secrets\\.env,
overridable with the CF_SECRETS_ENV env var) and exposed only as in-process values.
The per-page refresh_token/token stay in Dashboard/secrets/<page>/youtube.json
(path-only, gitignored); this module supplies the app creds that combine with them.

Nothing here logs the secret. Callers that fail to find creds get a CLEAR error
naming the missing keys and the .env path, not a confusing crash.
"""

import os

# Default location of the external secrets .env (off the system drive, outside repo).
DEFAULT_SECRETS_ENV = r"E:\Installed\ContentFactory-secrets\.env"

CLIENT_ID_KEY = "GOOGLE_OAUTH_CLIENT_ID"
CLIENT_SECRET_KEY = "GOOGLE_OAUTH_CLIENT_SECRET"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"


def secrets_env_path() -> str:
    """Absolute path to the external secrets .env (CF_SECRETS_ENV overrides the default)."""
    return os.getenv("CF_SECRETS_ENV", DEFAULT_SECRETS_ENV)


def load_oauth_env() -> None:
    """Load the external secrets .env into os.environ (does NOT override existing vars).

    No-op for the OAuth keys if they are already set in the process environment
    (e.g. injected by a parent process) — that takes precedence. If the file is
    absent we stay quiet here; get_oauth_app_credentials() raises the clear error
    only when the values are actually needed.
    """
    path = secrets_env_path()
    if not os.path.isfile(path):
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    # override=False: a value already in os.environ wins over the file.
    load_dotenv(path, override=False)


def get_oauth_app_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from the environment.

    Loads the external .env first. Raises RuntimeError with a clear, actionable
    message if either value is missing — naming both the env keys and the .env path
    so the owner knows exactly what to fix. The secret itself is never included in
    the message.
    """
    load_oauth_env()
    client_id = os.getenv(CLIENT_ID_KEY, "").strip()
    client_secret = os.getenv(CLIENT_SECRET_KEY, "").strip()
    if not client_id or not client_secret:
        path = secrets_env_path()
        missing = [k for k, v in ((CLIENT_ID_KEY, client_id), (CLIENT_SECRET_KEY, client_secret)) if not v]
        hint = "exists" if os.path.isfile(path) else "NOT found"
        raise RuntimeError(
            f"Google OAuth app credentials missing: {', '.join(missing)}. "
            f"Set {CLIENT_ID_KEY} / {CLIENT_SECRET_KEY} in the external secrets .env "
            f"(expected at: {path} — {hint}), or point CF_SECRETS_ENV at it. "
            "These app creds are intentionally NOT stored in the repo."
        )
    return client_id, client_secret
