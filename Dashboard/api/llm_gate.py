"""llm_gate — the ONE place that decides WHICH LLM serves a text script-gen call.

WHY THIS EXISTS
Every Vietnamese script/translation/hashtag prompt in this project used to go to exactly
one backend: Claude Code headless (`claude -p`, billed to the owner's subscription). If
that subscription ever goes away the pipeline stops. This module makes the backend a
PER-JOB CHOICE (`jobs.llm_provider` / `jobs.llm_model`) without changing anything about
how the default path behaves.

THE CRITICAL INVARIANT
`provider=None` (or "claude-cli") must be INDISTINGUISHABLE from the code that existed
before this module. It therefore does not re-implement the CLI leg: it calls straight
back into `generate._run_claude_script_once`, which still owns the exact Popen argv, the
process-tree registration for POST /api/jobs/{id}/stop, and the shared `_claude_result`
read/reap/error-classify body. Nothing about the default path is duplicated here.

WHAT IS AND IS NOT IN SCOPE
  * IN  — text script-gen: footage/transform/topic scene arrays, dubbed subtitle
          translation, filler detection, Facebook hashtags. All of these are a single
          "prompt in -> JSON array out" call, which is exactly what a plain
          OpenAI-compatible /chat/completions request gives you.
  * OUT — the two VISION calls (`generate._run_claude_vision_script`,
          `generate._vision_cover_prompt`). They work by handing Claude Code's Read tool
          a directory of frames and telling it to open them — agentic CLI behavior with
          no equivalent in a chat-completions request. They stay hard-wired to claude-cli
          and this module must never be pointed at them.

NO LADDER, NO SILENT FALLBACK (deliberate)
If the chosen provider fails after its own retry budget, the JOB FAILS with a message
naming the provider, the model and the reason. There is no automatic hop to another
provider. The owner must never discover after the fact that a different model wrote the
script. `LLMResult` still reports which provider/model actually served the call so a
future ladder phase has somewhere honest to put a different answer.

PROVIDER FACTS (measured in Phase 1 — see _workspace/13_research_openrouter_free_tier.md
and the A/B run driven by tools/llm_bench.py; do not re-litigate them here)
  * gemini     — `gemini-flash-latest` via the OpenAI-compat endpoint works reliably and
                 writes good Vietnamese. NOTE: `gemini-2.5-flash` 404s with "no longer
                 available to new users" on a fresh key even though ListModels still
                 advertises it, which is why the catalogue below is a FIXED allowlist that
                 is merely INTERSECTED with the live list (see list_model_options).
  * openrouter — every free model tested is a "thinking" model that burns 80%+ of its
                 token budget on chain-of-thought and then times out or under-delivers
                 (one run: 43% under the word budget, 57% short on scene count). Shipped
                 because the owner wants the option, flagged `reliability: "low"`
                 UNCONDITIONALLY, and never a default.

PRIVACY
Both free tiers reserve the right to train on what we send. Script-gen prompts carry
public transcripts and our own instructions — acceptable, and the owner accepted it for
the bench. `assert_no_secrets()` hard-refuses to send anything credential-shaped; that
guard is a security control, not a nicety. Do not remove it.

KNOWN LIMITATION (reported honestly, not silently accepted)
POST /api/jobs/{id}/stop kills the LLM step by hard-killing the `claude -p` process tree
(`generate.kill_job_processes`). An HTTP provider has no process to kill, so a Stop
pressed while a gemini/openrouter call is in flight only takes effect at the next step
boundary (bounded by the per-call timeout). Wiring an abort-token through httpx is a
follow-up, not part of this phase.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import HTTPException

log = logging.getLogger("contentfactory.llm_gate")

# httpx is a declared dependency (requirements.txt) but the CLI leg must keep working
# even if it is somehow missing — a broken optional provider must never take down the
# default path that the whole pipeline depends on.
try:
    import httpx
except ImportError:  # pragma: no cover - environment problem, not a bug
    httpx = None  # type: ignore[assignment]


# ==========================================================================================
# Identity
# ==========================================================================================

# The default provider. NULL/empty `jobs.llm_provider` means exactly this, everywhere.
PROVIDER_CLAUDE_CLI = "claude-cli"
PROVIDER_GEMINI = "gemini"
PROVIDER_OPENROUTER = "openrouter"

# Verified-callable Gemini models (measured 2026-07-30 with a fresh $0 key). This list is
# FIXED on purpose: Gemini's ListModels advertises models a new key gets 404/429 on, so
# trusting the live list would hand the owner an unusable option. The live list is used
# only to REMOVE entries that have been fully retired (see list_model_options).
GEMINI_MODELS = (
    "gemini-flash-latest",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
)
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", GEMINI_MODELS[0])

# OpenRouter has NO safe hardcoded default: the free catalogue churns weekly (Phase 1
# measured a catalogue sharing almost nothing with 2025-era lists). A job must therefore
# name a concrete model, which the Studio gets from GET /api/llm/models. This env var
# exists only as an escape hatch for a headless test.
OPENROUTER_DEFAULT_MODEL = (os.getenv("OPENROUTER_MODEL") or "").strip() or None

OPENROUTER_API = "https://openrouter.ai/api/v1"
GEMINI_OPENAI_API = "https://generativelanguage.googleapis.com/v1beta/openai"
GEMINI_NATIVE_API = "https://generativelanguage.googleapis.com/v1beta"

# Output budget for an HTTP leg. The CLI leg has no equivalent knob (Claude Code decides),
# so this is NEW-PATH-ONLY and cannot shift the default path's behavior. A footage batch is
# ~19-23 Vietnamese scenes; 16k output tokens is comfortable headroom.
LLM_MAX_OUTPUT_TOKENS = int(os.getenv("LLM_MAX_OUTPUT_TOKENS", "16384"))
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.7"))

# Extra output headroom for OpenRouter only. Phase 1 measured free "thinking" models
# spending ~2700 tokens on chain-of-thought BEFORE the answer, and `reasoning.max_tokens`
# being SILENTLY IGNORED by the free endpoint — so the only thing that helps is real
# headroom on top of the budget. `reasoning.exclude` DOES work and strips the visible
# reasoning text, so the parser only ever sees candidate JSON.
OPENROUTER_MAX_TOKENS_BONUS = int(os.getenv("OPENROUTER_MAX_TOKENS_BONUS", "3000"))

# How many discovered OpenRouter free models GET /api/llm/models surfaces (best-ranked
# first). Owner explicitly asked to see the full discovered set, not a trimmed list.
OPENROUTER_MAX_OPTIONS = int(os.getenv("OPENROUTER_MAX_OPTIONS", "12"))


# ==========================================================================================
# Errors — ONE taxonomy shared with the CLI leg
# ==========================================================================================

class LLMError(HTTPException):
    """A provider call failed.

    Subclasses HTTPException ON PURPOSE so it flows through every existing path unchanged:
    `_run_claude_script`'s `except HTTPException`, the runner's error persistence, and
    FastAPI's response mapping all keep working with no new branch. The extra fields are
    what a provider layer needs on top:

      `retryable`   — should `_run_claude_script` re-run this in a fresh attempt? Set by
                      `_classify_http` (429 + transient 5xx + network/timeouts) and read by
                      `generate._is_retryable`. This is an ADDITION to the CLI leg's
                      existing 504 rule, not a replacement for it.
      `provider` / `model` — which backend failed, so the failed job row names it.
      `http_status` — alias of `status_code`, spelled the way the provider docs spell it.
    """

    def __init__(self, status_code: int, detail: str, *, provider: str,
                 model: str | None = None, retryable: bool = False):
        super().__init__(status_code, detail)
        self.provider = provider
        self.model = model
        self.retryable = retryable

    @property
    def http_status(self) -> int:
        return self.status_code


def is_retryable(exc: BaseException) -> bool:
    """True when a failed call is worth ONE more attempt in a fresh request.

    HTTP legs: 429 (rate limit / free-tier cap), transient 5xx, connection reset, read
    timeout. NOT retryable: 400 (bad request), 401/403 (auth), 404 (model gone), 413
    (too large), 422 — those are contract errors a retry cannot fix, and on a 50-request/day
    free tier a retry storm is actively harmful.

    The CLI leg keeps its own rule (504 only) in `generate._is_retryable`; this function
    only answers for LLMError.
    """
    return isinstance(exc, LLMError) and bool(exc.retryable)


# Retryable HTTP statuses. 408 request-timeout and 409/425 are not produced by these
# providers; 529 is Anthropic-style "overloaded" and is included for the day an
# Anthropic-compatible leg is added.
_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524, 529}


def _classify_http(status: int, body: str, *, provider: str, model: str | None,
                   headers: dict | None = None) -> LLMError:
    """Turn a non-200 provider response into an LLMError with the right retryable flag."""
    snippet = (body or "").strip().replace("\n", " ")[:400]
    extra = ""
    if status == 429 and headers:
        # Surface the rate-limit headers verbatim — on a free tier the reset time is the
        # single most useful thing in the failed job row.
        bits = [f"{h}={headers[h]}" for h in
                ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset")
                if h in headers]
        if bits:
            extra = " | " + ", ".join(bits)
    return LLMError(
        status if status in (429, 401, 403, 404, 413) else 502,
        f"{provider}/{model or '?'} HTTP {status}: {snippet}{extra}",
        provider=provider, model=model,
        retryable=status in _RETRYABLE_STATUS,
    )


# ==========================================================================================
# Safety: never send anything credential-shaped to a third party
# ==========================================================================================

# A free tier may train on and even publish what we send. A script-gen prompt legitimately
# contains only public transcripts + our instructions, so a match here means something is
# wrong UPSTREAM and the request must not go out. Same patterns as tools/llm_bench.py.
_SECRET_PATTERNS = (
    (r"\bsk-[A-Za-z0-9_\-]{16,}", "OpenAI/Anthropic-style key"),
    (r"\bsk-or-v1-[A-Za-z0-9_\-]{16,}", "OpenRouter key"),
    (r"\bAIza[0-9A-Za-z_\-]{30,}", "Google API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"\bya29\.[A-Za-z0-9_\-]{20,}", "Google OAuth access token"),
    (r"\bEAA[A-Za-z0-9]{40,}", "Meta/Facebook access token"),
    (r"(?i)\b(refresh_token|client_secret|private_key|BEGIN [A-Z ]*PRIVATE KEY)\b",
     "credential field"),
    (r"(?i)Dashboard[\\/]secrets[\\/]", "path into Dashboard/secrets/"),
)


def assert_no_secrets(text: str, *, provider: str) -> None:
    """Refuse to send a prompt carrying anything credential-shaped. Raises LLMError(400).

    Applies ONLY to the outbound HTTP legs: the claude-cli leg talks to the owner's own
    subscription over Anthropic's first-party endpoint, which is not a third party in the
    sense this guard protects against, and adding a scan there would change the default
    path's behavior.
    """
    for pat, what in _SECRET_PATTERNS:
        if re.search(pat, text or ""):
            raise LLMError(
                400,
                f"REFUSING to send this prompt to {provider}: it looks like it contains a "
                f"{what}. Free tiers may train on and publish prompts — scrub it first.",
                provider=provider, retryable=False,
            )


# ==========================================================================================
# Result type
# ==========================================================================================

@dataclass
class LLMResult:
    """One successful LLM call.

    `provider` / `model` are what ACTUALLY served the call. Today they always equal what
    was requested (there is no fallback), but the field exists so a future ladder phase can
    report the leg that really answered without changing every caller.
    """
    data: list
    provider: str
    model: str | None
    raw: str = ""
    usage: dict = field(default_factory=dict)


# ==========================================================================================
# Resolution — provider/model ids, deterministic and network-free
# ==========================================================================================

def normalize_provider(provider: str | None) -> str:
    """NULL / empty / unknown-case -> the default provider id."""
    p = (provider or "").strip().lower()
    return p or PROVIDER_CLAUDE_CLI


def resolve(provider: str | None, model: str | None) -> tuple[str, str | None]:
    """(provider_id, model_id) for a job's stored choice.

    PURE and NETWORK-FREE by design. It is called on the cache-key path, so it must be
    deterministic — a resolution that did live model discovery would make the same job
    hash differently on different days and quietly split the script cache.

    NULL model means "the provider's own default":
      claude-cli -> generate.SCRIPT_GEN_MODEL (today's global model pin)
      gemini     -> GEMINI_DEFAULT_MODEL
      openrouter -> OPENROUTER_MODEL env if set, else None. None here is NOT an error yet;
                    `run_llm_json` raises a clear one, because a free-catalogue default
                    cannot be guessed safely (the ids churn weekly).
    """
    pid = normalize_provider(provider)
    mid = (model or "").strip() or None
    if mid:
        return pid, mid
    if pid == PROVIDER_CLAUDE_CLI:
        import generate  # lazy: generate imports this module at its top level
        return pid, generate.SCRIPT_GEN_MODEL
    if pid == PROVIDER_GEMINI:
        return pid, GEMINI_DEFAULT_MODEL
    if pid == PROVIDER_OPENROUTER:
        return pid, OPENROUTER_DEFAULT_MODEL
    return pid, None


def api_key_for(provider: str) -> str | None:
    """The provider's key from the environment, or None. Read at CALL time (not import) so
    a key added to .env takes effect on the next API restart without touching this file.
    Keys live only in the environment — never in the DB, never logged, never echoed."""
    if provider == PROVIDER_GEMINI:
        return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_AI_STUDIO_API_KEY") or "").strip() or None
    if provider == PROVIDER_OPENROUTER:
        return (os.getenv("OPENROUTER_API_KEY") or "").strip() or None
    return None


# ==========================================================================================
# Parsing — tolerant, but never inventive
# ==========================================================================================

_ARRAY_KEYS = ("scenes", "script", "data", "items", "result", "output")


def _parse_json_array(text: str, *, provider: str, model: str | None) -> list:
    """Pull the JSON array out of a raw completion.

    Mirrors `generate._extract_json_array` (fence strip -> first '[' .. last ']') and adds
    ONE thing the CLI leg never needed: OpenAI-style structured output requires an OBJECT
    root, so a model that was asked for an array may legitimately answer
    {"scenes": [...]}. Unwrapping that is not guessing — the array is right there.

    A truncated array is reported AS truncation, because "wrong shape" and "ran out of
    output tokens" send you to completely different fixes.
    """
    s = (text or "").strip()
    if not s:
        raise LLMError(502, f"{provider}/{model or '?'} returned an empty body",
                       provider=provider, model=model, retryable=True)

    m = re.search(r"```(?:json|JSON)?\s*(.*?)```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()

    def _try(candidate: str):
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            return None

    obj = _try(s)
    if obj is None:
        for lo_ch, hi_ch in (("[", "]"), ("{", "}")):
            lo, hi = s.find(lo_ch), s.rfind(hi_ch)
            if lo != -1 and hi > lo:
                obj = _try(s[lo:hi + 1])
                if obj is not None:
                    break

    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in _ARRAY_KEYS:
            if isinstance(obj.get(k), list):
                return obj[k]
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]

    if s.count("[") > s.count("]"):
        raise LLMError(
            502,
            f"{provider}/{model or '?'} output is TRUNCATED — unterminated JSON array "
            f"(raise LLM_MAX_OUTPUT_TOKENS or shrink the batch): {s[-200:]}",
            provider=provider, model=model, retryable=True,
        )
    raise LLMError(502, f"Could not parse script JSON from {provider}/{model or '?'}: {s[:300]}",
                   provider=provider, model=model, retryable=False)


# ==========================================================================================
# The HTTP leg — one OpenAI-compatible request shape for every non-CLI provider
# ==========================================================================================

def _http_provider_config(provider: str, model: str | None) -> dict:
    """Per-provider request details. Shapes mirror tools/llm_bench.py's PROVIDERS /
    _build_body / call_leg, which are the versions that were actually exercised against
    the live endpoints in Phase 1."""
    if provider == PROVIDER_GEMINI:
        return {
            "base_url": GEMINI_OPENAI_API,
            "extra_headers": {},
            # Gemini's OpenAI-compat layer takes the plain OpenAI body; no provider block.
            "extra_body": {},
            "max_tokens_bonus": 0,
        }
    if provider == PROVIDER_OPENROUTER:
        return {
            "base_url": OPENROUTER_API,
            "extra_headers": {
                # Attribution headers, per OpenRouter's API docs. No account data.
                "HTTP-Referer": "https://github.com/local/ContentFactory",
                "X-OpenRouter-Title": "ContentFactory",
            },
            # `require_parameters` is MANDATORY, not cosmetic: structured-output support is
            # a property of the ENDPOINT, and the same :free id can be served by one
            # endpoint that supports it and another that does not (Phase 1, measured).
            # `reasoning.exclude` strips visible chain-of-thought from the response.
            "extra_body": {"provider": {"require_parameters": True},
                           "reasoning": {"exclude": True}},
            "max_tokens_bonus": OPENROUTER_MAX_TOKENS_BONUS,
        }
    raise LLMError(400, f"Unknown LLM provider '{provider}'. Known: "
                        f"{PROVIDER_CLAUDE_CLI}, {PROVIDER_GEMINI}, {PROVIDER_OPENROUTER}.",
                   provider=provider, model=model, retryable=False)


def _call_http_json(prompt: str, *, system_prompt: str | None, timeout: int,
                    provider: str, model: str) -> LLMResult:
    """One OpenAI-compatible /chat/completions request -> parsed JSON array.

    Never falls back to another provider or another model. Every failure raises LLMError
    carrying the provider/model that failed and whether it is worth one more attempt.
    """
    if httpx is None:  # pragma: no cover
        raise LLMError(500, "httpx is not installed in the API venv — "
                            "`pip install -r Dashboard/api/requirements.txt`",
                       provider=provider, model=model, retryable=False)

    key = api_key_for(provider)
    if not key:
        env = "GEMINI_API_KEY" if provider == PROVIDER_GEMINI else "OPENROUTER_API_KEY"
        raise LLMError(
            400,
            f"Job requested LLM provider '{provider}' but {env} is not set in "
            f"Dashboard/api/.env. Refusing to run — a missing key must NEVER silently "
            f"fall back to another provider.",
            provider=provider, model=model, retryable=False,
        )

    cfg = _http_provider_config(provider, model)
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    body: dict = {
        "model": model,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_OUTPUT_TOKENS + int(cfg["max_tokens_bonus"]),
        "stream": False,
    }
    body.update(cfg["extra_body"])

    # Security control — see assert_no_secrets. Scans the exact text that goes out.
    assert_no_secrets(json.dumps(messages, ensure_ascii=False), provider=provider)

    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    headers.update(cfg["extra_headers"])
    url = f"{cfg['base_url'].rstrip('/')}/chat/completions"

    t0 = time.monotonic()
    try:
        # follow_redirects: Gemini's compat layer has redirected in the past.
        with httpx.Client(follow_redirects=True) as client:
            r = client.post(url, headers=headers, json=body, timeout=timeout)
    except httpx.TimeoutException:
        raise LLMError(504, f"{provider}/{model} timed out after {timeout}s",
                       provider=provider, model=model, retryable=True)
    except httpx.HTTPError as exc:
        # Connection reset / DNS / TLS — transient by nature, worth one more attempt.
        raise LLMError(502, f"{provider}/{model} transport error: {type(exc).__name__}: {exc}",
                       provider=provider, model=model, retryable=True)

    if r.status_code != 200:
        raise _classify_http(r.status_code, r.text, provider=provider, model=model,
                             headers=dict(r.headers))

    try:
        data = r.json()
    except ValueError as exc:
        raise LLMError(502, f"{provider}/{model} returned a non-JSON envelope: {exc}",
                       provider=provider, model=model, retryable=True)

    choices = data.get("choices") or []
    msg = (choices[0].get("message") or {}) if choices else {}
    raw = msg.get("content") or ""
    finish = choices[0].get("finish_reason") if choices else None
    served_model = data.get("model") or model

    if not raw:
        # A reasoning model that spent its whole budget thinking lands here with
        # content=null and finish_reason='length'. Say so — it is a budget problem.
        raise LLMError(
            502,
            f"{provider}/{served_model} returned no content "
            f"(finish_reason={finish!r}) — a reasoning model likely spent its entire "
            f"token budget on chain-of-thought.",
            provider=provider, model=served_model, retryable=True,
        )

    scenes = _parse_json_array(raw, provider=provider, model=served_model)
    if finish and finish not in ("stop", "STOP"):
        # Parsed anyway (the array happened to be complete), but record the anomaly —
        # 'length' here means the next batch may well be truncated.
        log.warning("[llm] %s/%s finish_reason=%s but the JSON parsed",
                    provider, served_model, finish)

    log.info("[llm] %s/%s call done in %.1fs (%d elements)",
             provider, served_model, time.monotonic() - t0,
             len(scenes) if isinstance(scenes, list) else 0)
    return LLMResult(data=scenes, provider=provider, model=served_model, raw=raw,
                     usage=data.get("usage") or {})


# ==========================================================================================
# THE entry point
# ==========================================================================================

def run_llm_json(prompt: str, *, timeout: int, system_prompt: str | None = None,
                 provider: str | None = None, model: str | None = None) -> LLMResult:
    """Run ONE text prompt on the chosen provider and return the parsed JSON array.

    `provider=None` / `"claude-cli"` is the DEFAULT PATH and is byte-for-byte the old
    behavior: it calls `generate._run_claude_script_once`, which builds the same Popen
    argv, registers the same process tree for POST /stop, and goes through the same
    `_claude_result` error taxonomy (`error_max_turns` -> 504 -> retryable). This function
    adds NOTHING to that path — no extra parsing, no extra retry, no extra logging.

    Retries and the disk cache deliberately live ABOVE this function, in
    `generate._run_claude_script`, so both legs share exactly one retry policy, one cache,
    one spell-fix pass and one set of log tags. Do not add a second retry loop here.

    `system_prompt` is used only by the HTTP legs. The CLI leg passes its own
    `SCRIPT_GEN_SYSTEM_PROMPT` via `--system-prompt` inside `_run_claude_script_once`;
    duplicating it here would change the argv the default path builds.
    """
    pid, mid = resolve(provider, model)

    if pid == PROVIDER_CLAUDE_CLI:
        import generate  # lazy: generate imports this module at its top level
        # `mid` is generate.SCRIPT_GEN_MODEL unless the job pinned something else, and
        # `_run_claude_script_once` treats None/that value identically -> same argv.
        scenes = generate._run_claude_script_once(prompt, timeout, model=mid)
        return LLMResult(data=scenes, provider=pid, model=mid)

    if pid in (PROVIDER_GEMINI, PROVIDER_OPENROUTER):
        if not mid:
            raise LLMError(
                400,
                f"Provider '{pid}' needs an explicit model — its free catalogue changes "
                f"too often to have a safe hardcoded default. Pick one from "
                f"GET /api/llm/models (or set OPENROUTER_MODEL in .env).",
                provider=pid, retryable=False,
            )
        return _call_http_json(prompt, system_prompt=system_prompt, timeout=timeout,
                               provider=pid, model=mid)

    raise LLMError(400, f"Unknown LLM provider '{pid}'. Known: {PROVIDER_CLAUDE_CLI}, "
                        f"{PROVIDER_GEMINI}, {PROVIDER_OPENROUTER}.",
                   provider=pid, model=mid, retryable=False)


# ==========================================================================================
# Catalogue for GET /api/llm/models — discovery + a 6h on-disk cache
# ==========================================================================================

# Same mechanics and same directory convention as generate.py's _script_cache: a plain
# JSON file next to the API module, mtime as the TTL clock, every read/write failure
# treated as a miss so the cache can never break the endpoint.
_LLM_CACHE_DIR = os.path.join(os.path.dirname(__file__), "_llm_cache")
_MODELS_CACHE_TTL_HOURS = float(os.getenv("LLM_MODELS_CACHE_TTL_HOURS", "6"))


def _models_cache_path() -> str:
    return os.path.join(_LLM_CACHE_DIR, "models.json")


def _models_cache_get() -> dict | None:
    path = _models_cache_path()
    try:
        if not os.path.isfile(path):
            return None
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        if age_h >= _MODELS_CACHE_TTL_HOURS:
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _models_cache_put(payload: dict) -> None:
    try:
        os.makedirs(_LLM_CACHE_DIR, exist_ok=True)
        with open(_models_cache_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception as e:  # noqa: BLE001 — cache is advisory only
        log.warning("[llm] models cache write failed: %s", e)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _live_gemini_models(key: str, timeout: int = 20) -> set[str] | None:
    """Bare model ids Gemini's ListModels reports for THIS key, or None if unreachable.

    Used ONLY to subtract fully-retired entries from GEMINI_MODELS. It is explicitly NOT
    used to ADD options: Phase 1 measured ListModels advertising models a fresh key gets
    404 'no longer available to new users' on, so presence in this list proves nothing
    about callability — the fixed allowlist is what defends against that.
    """
    if httpx is None:
        return None
    try:
        with httpx.Client(follow_redirects=True) as client:
            r = client.get(f"{GEMINI_NATIVE_API}/models", params={"key": key},
                           timeout=timeout)
        if r.status_code != 200:
            log.warning("[llm] Gemini ListModels HTTP %s — keeping the full allowlist",
                        r.status_code)
            return None
        names = set()
        for m in (r.json() or {}).get("models") or []:
            name = str(m.get("name") or "")
            names.add(name.split("/", 1)[-1] if "/" in name else name)
        return names
    except Exception as e:  # noqa: BLE001 — discovery must never fail the endpoint
        log.warning("[llm] Gemini ListModels failed (%s) — keeping the full allowlist", e)
        return None


def _live_openrouter_free(limit: int = 12, min_context: int = 32768,
                          timeout: int = 30) -> list[dict]:
    """Discover OpenRouter's $0 models and read each one's FREE-ENDPOINT capabilities.

    Same logic as tools/llm_bench.py's discover_openrouter_free/pick_openrouter_model,
    written fresh for the API. Why per-ENDPOINT and not per-model: structured-output
    support and context length belong to the endpoint, and the models-list headline
    numbers describe the BEST endpoint — which may not be the free one (Phase 1 measured
    one :free id served by a 131k structured-capable endpoint AND a 262k incapable one).

    Both /models and /models/<id>/endpoints are unauthenticated, so this consumes no free
    request quota. Returns [] on any failure — the endpoint then simply lists no
    OpenRouter options rather than failing.
    """
    if httpx is None:
        return []
    out: list[dict] = []
    try:
        with httpx.Client(follow_redirects=True) as client:
            r = client.get(f"{OPENROUTER_API}/models", timeout=timeout)
            if r.status_code != 200:
                log.warning("[llm] OpenRouter /models HTTP %s", r.status_code)
                return []
            models = (r.json() or {}).get("data") or []
            free = [m for m in models
                    if _num((m.get("pricing") or {}).get("prompt"), -1) == 0.0
                    and _num((m.get("pricing") or {}).get("completion"), -1) == 0.0]
            # :free variants first, then widest headline context.
            free.sort(key=lambda m: (0 if str(m.get("id", "")).endswith(":free") else 1,
                                     -_num((m.get("top_provider") or {}).get("context_length"))))
            for m in free[:limit]:
                mid = str(m.get("id") or "")
                if not mid:
                    continue
                row = {"id": mid, "ctx": 0, "structured": False, "uptime_1d": 0.0}
                try:
                    er = client.get(f"{OPENROUTER_API}/models/{mid}/endpoints", timeout=timeout)
                    if er.status_code == 200:
                        for ep in ((er.json() or {}).get("data") or {}).get("endpoints") or []:
                            epr = ep.get("pricing") or {}
                            if not (_num(epr.get("prompt"), -1) == 0.0
                                    and _num(epr.get("completion"), -1) == 0.0):
                                continue  # a PAID endpoint of a model that also has a free one
                            sp = ep.get("supported_parameters") or []
                            ctx = int(_num(ep.get("context_length")))
                            row["ctx"] = max(row["ctx"], ctx)
                            row["structured"] = row["structured"] or ("structured_outputs" in sp)
                            row["uptime_1d"] = max(row["uptime_1d"],
                                                   _num(ep.get("uptime_last_1d")))
                except httpx.HTTPError:
                    pass
                # Usable = a free endpoint big enough for a long transcript. Structured
                # output is recorded but NOT required: this pipeline asks for JSON in the
                # prompt and parses tolerantly, exactly as the claude-cli leg does.
                if row["ctx"] >= min_context:
                    out.append(row)
                time.sleep(0.1)  # be polite to a public unauthenticated endpoint
    except Exception as e:  # noqa: BLE001 — discovery must never fail the endpoint
        log.warning("[llm] OpenRouter discovery failed: %s", e)
        return []
    # Reliability first (a free endpoint at 93.7%/1d fails ~1 request in 16), then context.
    out.sort(key=lambda r: (-r["uptime_1d"], -r["ctx"]))
    return out


def list_model_options(force_refresh: bool = False) -> dict:
    """The payload behind GET /api/llm/models.

    {"options": [{provider, model, label, is_default, reliability, notes}, ...],
     "generated_at": "<iso8601>", "cached": bool}

    Rules, all deliberate:
      * claude-cli is ALWAYS present and is ALWAYS the only `is_default: true` entry. It
        needs no key check — it is the existing subscription path.
      * gemini appears only when GEMINI_API_KEY is set, and only for allowlist entries the
        live ListModels still knows about (retirement defence). If ListModels is
        unreachable the full allowlist is kept — an outage must not empty the menu.
      * openrouter appears only when OPENROUTER_API_KEY is set, is discovered LIVE (the
        free ids churn weekly), and every entry is `reliability: "low"` UNCONDITIONALLY
        per the Phase 1 measurements, whatever the discovery says.
    """
    if not force_refresh:
        cached = _models_cache_get()
        if cached:
            return {**cached, "cached": True}

    options: list[dict] = [{
        "provider": PROVIDER_CLAUDE_CLI,
        "model": None,
        "label": "Claude (subscription)",
        "is_default": True,
        "reliability": "high",
        "notes": ("Claude Code headless (`claude -p`), billed to the owner's "
                  "subscription. The default and the only path the pipeline's prompts, "
                  "pace and word budgets are calibrated on."),
    }]

    gkey = api_key_for(PROVIDER_GEMINI)
    if gkey:
        live = _live_gemini_models(gkey)
        for mid in GEMINI_MODELS:
            if live is not None and mid not in live:
                log.info("[llm] Gemini model %s is no longer listed — hiding it", mid)
                continue
            options.append({
                "provider": PROVIDER_GEMINI,
                "model": mid,
                "label": f"Gemini {mid.replace('gemini-', '')} (Google AI Studio)",
                "is_default": False,
                "reliability": "high",
                "notes": ("Free tier via the OpenAI-compatible endpoint. Verified callable "
                          "and produces valid JSON + good Vietnamese. Google's free tier "
                          "may train on prompts."),
            })

    okey = api_key_for(PROVIDER_OPENROUTER)
    if okey:
        # Top-N only. Discovery routinely finds ~12 free models; listing all of them would
        # make a menu whose majority is flagged low-reliability, burying the two options
        # the owner should actually reach for. They are already sorted reliability-first.
        for row in _live_openrouter_free()[:OPENROUTER_MAX_OPTIONS]:
            options.append({
                "provider": PROVIDER_OPENROUTER,
                "model": row["id"],
                "label": f"OpenRouter {row['id']}",
                "is_default": False,
                # UNCONDITIONAL, whatever discovery reports. See the Phase 1 note.
                "reliability": "low",
                "notes": ("Free-tier reasoning models often fail to complete within budget "
                          "— treat as experimental fallback. "
                          f"Free endpoint ctx {row['ctx']:,}, 1d uptime "
                          f"{row['uptime_1d']:.1f}%, structured_outputs="
                          f"{'yes' if row['structured'] else 'no'}. "
                          "$0 tier is 20 req/min and 50 req/day (UTC reset)."),
            })

    payload = {"options": options,
               "generated_at": datetime.now(timezone.utc).isoformat()}
    _models_cache_put(payload)
    return {**payload, "cached": False}
