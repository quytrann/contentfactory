#!/usr/bin/env python
r"""llm_bench.py — A/B measurement harness for FREE-TIER LLMs on Vietnamese script-gen.

WHAT THIS IS FOR
ContentFactory generates its Vietnamese narration scripts with `claude -p` (Claude Code
headless, billed to a subscription). If that subscription goes away we need a $0
replacement. No public benchmark answers "which free model writes the best Vietnamese
long-form narration" — see _workspace/13_research_openrouter_free_tier.md §3. So this
tool runs ONE FIXED script-gen task across several candidate providers and puts the
objective numbers side by side, while saving the raw Vietnamese to disk so the OWNER can
read it and judge fluency himself. The tool does NOT rank fluency; it cannot.

WHAT IT IS NOT
  * Not part of the pipeline. It deliberately does NOT import Dashboard/api/generate.py —
    it is standalone so it can never break, or be broken by, production script-gen.
    The prompts + expected scene shape are COPIES under tools/_dummy_data/llm_bench/.
  * Not a paid path. Every leg is a $0 tier. Keys are read from the environment only —
    this tool never creates an account, never creates a key, and has no notion of a
    balance or a top-up. A missing key SKIPS that leg; it never silently falls back.

QUICK START (needs httpx -> use the API venv, not cf-venv)

  # 1. No keys needed: live free-model discovery against OpenRouter's public /models
  Dashboard\api\.venv\Scripts\python.exe tools\llm_bench.py --list-models

  # 2. No keys needed: exercise parsing + scoring on saved fixture responses
  Dashboard\api\.venv\Scripts\python.exe tools\llm_bench.py --dry-run

  # 3. The real thing (whatever keys are set in the environment get benched)
  Dashboard\api\.venv\Scripts\python.exe tools\llm_bench.py

  # a single task, 3 samples each, only two legs
  ...llm_bench.py --tasks footage_recap_600s --repeat 3 --providers gemini,openrouter

Env vars, key signup URLs, and how to read the table: tools/README-llm-bench.md

RATE-LIMIT HYGIENE (on by default)
Sequential, one request at a time, paced to --rpm (default 20 = OpenRouter's free RPM).
Repeated 429s abort the WHOLE run with a message about the 50-requests-per-DAY cap,
because at 50 RPD a retry storm burns the owner's entire daily quota in a minute.

PRIVACY (from the research report, §1.6 / §2.1)
Free tiers of BOTH Gemini and OpenRouter reserve the right to train on what you send.
The owner has accepted that for this bench. Everything sent is a script-gen prompt over
PUBLIC source material. `assert_no_secrets()` below hard-refuses to send a prompt that
looks like it carries a token/key/credential path — do not remove it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Vietnamese narration must print without UnicodeEncodeError on the Windows console
# (cp1252). Force UTF-8 on our streams (PYTHONUTF8=1 also does this when set).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

try:
    import httpx
except ImportError:  # pragma: no cover - environment problem, not a bug
    sys.stderr.write(
        "llm_bench needs httpx. Run it with the API venv python, which already has it:\n"
        r"  Dashboard\api\.venv\Scripts\python.exe tools\llm_bench.py ..." "\n"
        "or: pip install 'httpx>=0.27,<0.29'\n")
    raise SystemExit(3)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIXTURE_DIR = os.path.join(_REPO_ROOT, "tools", "_dummy_data", "llm_bench")
_TASK_DIR = os.path.join(_FIXTURE_DIR, "tasks")
_SCHEMA_DIR = os.path.join(_FIXTURE_DIR, "schemas")
_RESPONSE_DIR = os.path.join(_FIXTURE_DIR, "responses")


def _load_env_file(path: str) -> None:
    """Populate os.environ from a KEY=VALUE .env file WITHOUT overriding anything already
    set in the real environment. Same helper as tools/voice_doctor.py. Used ONLY to pick
    up CONTENT_OUTPUT_ROOT (where bench artifacts go) — LLM API keys are expected in the
    real environment, not in the API's .env, and are never written anywhere by this tool."""
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


_load_env_file(os.path.join(_REPO_ROOT, "Dashboard", "api", ".env"))

# --- Output location ----------------------------------------------------------------
# Bench artifacts are MEDIA-like: bulky, regenerable, and they must never land in the
# repo. They follow the project's output convention (E:\ContentFactory\<...>), under a
# _bench sibling of the page dirs. --out-dir overrides; an in-repo override is covered
# by the `_bench_out/` rule in the root .gitignore.
CONTENT_OUTPUT_ROOT = os.getenv("CONTENT_OUTPUT_ROOT", r"E:\ContentFactory")
DEFAULT_OUT_ROOT = os.path.join(CONTENT_OUTPUT_ROOT, "_bench", "llm")

# Pace estimate. Copied (frozen) from generate.py::_VI_WORDS_PER_SEC on 2026-07-30 so the
# est. narration seconds this tool reports lines up with what the pipeline assumes. Not
# imported — see the module docstring.
VI_WORDS_PER_SEC = float(os.getenv("LLM_BENCH_WORDS_PER_SEC", "2.2"))

# Default per-request wall-clock ceiling. Long-transcript legs on a congested free
# endpoint are genuinely slow; a timeout is measured and reported, not retried.
DEFAULT_TIMEOUT = int(os.getenv("LLM_BENCH_TIMEOUT", "300"))

OPENROUTER_API = "https://openrouter.ai/api/v1"


# ==================================================================================
# Providers — every leg speaks the OpenAI-compatible /chat/completions shape.
# ==================================================================================

@dataclass
class Provider:
    key: str
    label: str
    base_url: str
    # First env var that is set wins. Distinct per provider on purpose: one leg's key can
    # never be used for another leg.
    key_envs: tuple[str, ...]
    signup_url: str
    # Default model. `None` = must be discovered at runtime (OpenRouter) or probed (Ollama).
    default_model: str | None = None
    needs_key: bool = True
    extra_body: dict = field(default_factory=dict)
    # Added ON TOP of task.max_tokens (not a replacement) — headroom for reasoning-model
    # chain-of-thought that a provider may not actually cap even when asked (see the
    # openrouter provider's comment: reasoning.max_tokens was measured to be silently
    # ignored by the one account-callable free model). 0 = no extra headroom.
    max_tokens_bonus: int = 0
    extra_headers: dict = field(default_factory=dict)
    note: str = ""

    def api_key(self) -> str | None:
        for env in self.key_envs:
            v = (os.getenv(env) or "").strip()
            if v:
                return v
        return None

    def __post_init__(self) -> None:
        # LLM_BENCH_<PROVIDER>_BASE_URL redirects a leg's OpenAI-compatible base URL.
        # Point it at a local proxy or a stub server (that is how the request shape is
        # verified offline). It must stay OpenAI-compatible and MUST stay a $0 endpoint —
        # this is not a hook for pointing a leg at a paid API.
        override = (os.getenv(f"LLM_BENCH_{self.key.upper()}_BASE_URL") or "").strip()
        if override:
            self.base_url = override.rstrip("/")
            self.note = (self.note + " [base URL overridden by env]").strip()


PROVIDERS: dict[str, Provider] = {
    # Google AI Studio's OpenAI-compatibility layer. Research report §2.1: strongest
    # free candidate (highest RPD, 1M-class context, first-class structured output).
    # Google no longer publishes the free-tier numbers — they must be observed here.
    "gemini": Provider(
        key="gemini",
        label="Gemini (Google AI Studio)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_envs=("GEMINI_API_KEY", "GOOGLE_AI_STUDIO_API_KEY"),
        signup_url="https://aistudio.google.com/apikey",
        default_model=os.getenv("LLM_BENCH_GEMINI_MODEL") or "gemini-flash-latest",
        note="Free tier trains on prompts (official). Pro models are NOT reliably free — "
             "keep to Flash / Flash-Lite. Override the model with --models gemini=<id>. "
             "MEASURED 2026-07-30 with a fresh $0 key: gemini-2.5-flash and "
             "gemini-2.5-flash-lite return 404 'no longer available to new users' and "
             "gemini-2.0-flash returns 429 (zero free quota), even though ListModels still "
             "advertises all three — so listing a model does NOT mean a new key may call it. "
             "Verified callable (HTTP 200): gemini-flash-latest, gemini-3.6-flash, "
             "gemini-3.5-flash, gemini-3.1-flash-lite. The default is the ALIAS so it keeps "
             "working through the next retirement; pin a concrete id for a reproducible A/B.",
    ),
    # OpenRouter. `require_parameters` is MANDATORY, not optional: structured-output
    # support is a property of the ENDPOINT, and the same :free model ID can be served by
    # one endpoint that supports it and another that does not (report §1.7, measured).
    # Without it a request can silently land on an incapable endpoint.
    "openrouter": Provider(
        key="openrouter",
        label="OpenRouter (:free)",
        base_url=OPENROUTER_API,
        key_envs=("OPENROUTER_API_KEY",),
        signup_url="https://openrouter.ai/settings/keys",
        default_model=os.getenv("LLM_BENCH_OPENROUTER_MODEL"),  # else: discovered live
        # MEASURED 2026-07-30: inclusionai/ling-3.0-flash:free (the only account-callable
        # free model that day, given the current privacy-toggle scope) burned its ENTIRE
        # task.max_tokens=4096 on visible word-by-word reasoning text and never emitted
        # the JSON answer (finish_reason=length, reasoning_tokens~2690, content=null).
        # `reasoning.max_tokens` (OpenRouter's unified cap) was TRIED and measured to be
        # SILENTLY IGNORED by this endpoint — two runs both still spent ~2690 reasoning
        # tokens regardless of the cap. So instead of capping (which this endpoint won't
        # honor), max_tokens_bonus below just adds real headroom on top of each task's
        # budget so the reasoning can run its course AND still leave room for the JSON.
        # `reasoning.exclude: true` DOES work (measured) — it strips the reasoning text
        # from the response so the parser only ever sees candidate JSON.
        extra_body={"provider": {"require_parameters": True},
                    "reasoning": {"exclude": True}},
        max_tokens_bonus=3000,
        extra_headers={
            "HTTP-Referer": "https://github.com/local/ContentFactory",
            "X-OpenRouter-Title": "ContentFactory llm_bench",
        },
        note="$0 tier = 20 RPM / 50 RPD (UTC-day reset). Model is DISCOVERED live — free "
             "model IDs churn weekly, so nothing is hardcoded. +3000 max_tokens headroom "
             "for reasoning-model chain-of-thought that this endpoint won't let us cap.",
    ),
    # Z.ai / Zhipu GLM. Report §2.7: GLM-*-Flash are listed Free on the official pricing
    # page (a standing $0 tier, not a trial), but context/RPD/structured-output support
    # are all UNVERIFIED — this bench is how they get verified.
    "zai": Provider(
        key="zai",
        label="Z.ai GLM (Flash, free)",
        base_url="https://api.z.ai/api/paas/v4",
        key_envs=("ZAI_API_KEY", "Z_AI_API_KEY"),
        signup_url="https://z.ai/model-api",
        default_model=os.getenv("LLM_BENCH_ZAI_MODEL") or "glm-4.5-flash",
        note="response_format support is UNVERIFIED; the harness auto-falls back to a "
             "prompt-only JSON request and records that it had to.",
    ),
    # Local Ollama. No key. Skipped cleanly when the daemon is not running.
    "ollama": Provider(
        key="ollama",
        label="Ollama (local)",
        base_url=(os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/") + "/v1",
        key_envs=(),
        signup_url="(local — no key)",
        default_model=os.getenv("LLM_BENCH_OLLAMA_MODEL"),  # else: first installed model
        needs_key=False,
        note="Shares the 8GB GPU with SDXL/TTS — expect a load-time penalty and a small "
             "usable context. Skipped silently if the daemon is not up.",
    ),
}


# ==================================================================================
# Safety: never send anything that looks like a credential
# ==================================================================================

# The prompt goes to a third party whose free tier may train on and even publish it.
# Script-gen prompts legitimately contain only public transcripts + instructions, so a
# match here means something is wrong upstream and the request must NOT go out.
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


def assert_no_secrets(text: str, where: str) -> None:
    """Refuse to send a prompt carrying anything credential-shaped. Raises ValueError."""
    for pat, what in _SECRET_PATTERNS:
        if re.search(pat, text or ""):
            raise ValueError(
                f"REFUSING to send {where}: it looks like it contains a {what}. "
                "Free tiers may train on and publish prompts — scrub it first.")


# ==================================================================================
# Tasks
# ==================================================================================

@dataclass
class Task:
    id: str
    label: str
    prompt: str
    system_prompt: str
    schema: dict | None
    max_tokens: int
    expect: dict

    @property
    def shape(self) -> str:
        return self.expect.get("shape") or "topic"


def load_tasks(only: list[str] | None) -> list[Task]:
    """Load *.task.json manifests from the fixture dir."""
    out: list[Task] = []
    if not os.path.isdir(_TASK_DIR):
        raise SystemExit(f"no task fixtures at {_TASK_DIR}")
    for fn in sorted(os.listdir(_TASK_DIR)):
        if not fn.endswith(".task.json"):
            continue
        man = json.loads(Path(os.path.join(_TASK_DIR, fn)).read_text(encoding="utf-8"))
        tid = man.get("id") or fn[: -len(".task.json")]
        if only and tid not in only:
            continue
        prompt = Path(os.path.join(_TASK_DIR, man["prompt_file"])).read_text(encoding="utf-8")
        schema = None
        if man.get("schema_file"):
            schema = json.loads(
                Path(os.path.join(_SCHEMA_DIR, man["schema_file"])).read_text(encoding="utf-8"))
        out.append(Task(
            id=tid, label=man.get("label") or tid, prompt=prompt,
            system_prompt=man.get("system_prompt") or "",
            schema=schema, max_tokens=int(man.get("max_tokens") or 8192),
            expect=man.get("expect") or {},
        ))
    if only:
        missing = set(only) - {t.id for t in out}
        if missing:
            raise SystemExit(f"unknown task id(s): {', '.join(sorted(missing))}")
    return out


def task_from_prompt_file(path: str, shape: str, scenes: int, words: int,
                          duration: int) -> Task:
    """Build a one-off task from a REAL prompt captured out of a past job."""
    prompt = Path(path).read_text(encoding="utf-8")
    req_keys = (["scene", "narration", "sourceStart", "sourceEnd"] if shape == "footage"
                else ["scene", "narration", "image_prompt"])
    schema_file = "scene_footage.schema.json" if shape == "footage" else "scene_topic.schema.json"
    schema = json.loads(Path(os.path.join(_SCHEMA_DIR, schema_file)).read_text(encoding="utf-8"))
    return Task(
        id="custom_" + re.sub(r"\W+", "_", Path(path).stem)[:40],
        label=f"custom prompt: {Path(path).name}",
        prompt=prompt,
        system_prompt="You output ONLY a valid JSON array of video script scenes. "
                      "No prose, no markdown, no code fences, no explanation — "
                      "just the JSON array.",
        schema=schema, max_tokens=16384,
        expect={"shape": shape, "required_keys": req_keys, "scene_count": scenes,
                "scene_count_tolerance": max(0, round(scenes * 0.25)),
                "word_budget": words, "word_tolerance_pct": 10,
                "target_duration_sec": duration},
    )


# ==================================================================================
# Parsing — accept anything a real provider plausibly emits, then judge it
# ==================================================================================

_ARRAY_KEYS = ("scenes", "script", "data", "items", "result", "output")


def extract_scene_list(text: str) -> list:
    """Pull the scene array out of a raw completion.

    Tolerant on purpose: this mirrors what a production gate would have to survive, and a
    leg that needed fence-stripping is still a usable leg (we record that it did). Handles
    (a) a bare JSON array, (b) a ```json fenced block, (c) an object wrapper such as
    {"scenes": [...]} — which is what strict json_schema mode produces, since OpenAI-style
    structured output requires an object root. Raises ValueError if no array is found.
    """
    s = (text or "").strip()
    if not s:
        raise ValueError("empty response body")
    # Strip a markdown fence, keeping only its contents.
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
        # Slice out the widest bracketed span and retry (drops chatty pre/postamble).
        for lo_ch, hi_ch in (("[", "]"), ("{", "}")):
            lo, hi = s.find(lo_ch), s.rfind(hi_ch)
            if lo != -1 and hi > lo:
                obj = _try(s[lo:hi + 1])
                if obj is not None:
                    break
    if obj is None:
        raise ValueError("response is not parseable JSON")
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in _ARRAY_KEYS:
            if isinstance(obj.get(k), list):
                return obj[k]
        # Single-key object wrapping the array under an unexpected name.
        lists = [v for v in obj.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        # A TRUNCATED array degrades to this: the widest [..] slice failed, so the {..}
        # fallback recovered only the first element as a lone object. Say so explicitly —
        # truncation (finish_reason=length, max_tokens too low, or a free endpoint cutting
        # the stream) is a completely different problem from "wrong shape", and confusing
        # the two sends the owner chasing the prompt instead of the token budget.
        if "[" in s and s.count("[") > s.count("]"):
            raise ValueError("output is TRUNCATED — unterminated JSON array "
                             "(check finish_reason / max_tokens)")
    raise ValueError(f"parsed JSON is a {type(obj).__name__}, not a scene array")


# ==================================================================================
# Scoring
# ==================================================================================

# Latin letters carrying a Vietnamese diacritic. A narration token containing one of
# these is Vietnamese with high confidence.
_VN_DIACRITICS = set("àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩị"
                     "òóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ"
                     "ÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊ"
                     "ÒÓỎÕỌÔỒỐỔỖỘƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴĐ")
# Undiacriticked Vietnamese function words — common enough that ignoring them would
# under-count Vietnamese in short lines.
_VN_PLAIN = {"va", "va", "la", "co", "khong", "cua", "cho", "trong", "ra", "vao", "nay",
             "do", "de", "ma", "thi", "ban", "no", "anh", "toi", "ta", "ho", "hon",
             "mot", "hai", "ba", "khi", "nhu", "voi", "tren", "duoi", "sau", "truoc",
             "game", "con", "nen", "cung", "hay", "ai", "gi", "sao", "the", "roi"}


def _words(s: str) -> list[str]:
    """Whitespace tokens. Vietnamese is written with space-separated syllables, so this is
    the same counting rule generate.py's word budget uses — the two stay comparable."""
    return [w for w in re.split(r"\s+", (s or "").strip()) if w]


def _vn_share(text: str) -> float:
    """HEURISTIC share of narration tokens that look Vietnamese. Not a language detector —
    it exists to catch the hard failure "the model answered in English", which is a
    disqualifier for this pipeline. A high-quality Vietnamese script that legitimately
    keeps English proper nouns will sit somewhere around 0.75-0.95, not 1.00."""
    toks = _words(text)
    if not toks:
        return 0.0
    hits = 0
    for t in toks:
        core = "".join(ch for ch in t if ch.isalpha())
        if not core:
            hits += 1  # numbers / punctuation: language-neutral, don't penalise
            continue
        if any(ch in _VN_DIACRITICS for ch in core):
            hits += 1
        elif unicodedata.normalize("NFD", core.lower()).encode("ascii", "ignore").decode() in _VN_PLAIN:
            hits += 1
    return hits / len(toks)


def score(scenes: list, task: Task) -> dict:
    """Objective metrics for one parsed scene list. No fluency judgement — that is the
    owner's job, which is why the raw text is written to disk."""
    exp = task.expect
    req_keys = exp.get("required_keys") or ["scene", "narration"]
    problems: list[str] = []

    bad = 0
    narr_parts: list[str] = []
    for i, el in enumerate(scenes):
        if not isinstance(el, dict):
            bad += 1
            continue
        if any(k not in el for k in req_keys):
            bad += 1
            continue
        n = el.get("narration")
        if not isinstance(n, str) or not n.strip():
            bad += 1
            continue
        narr_parts.append(n)
    shape_ok = bool(scenes) and bad == 0
    if not scenes:
        problems.append("0 scenes")
    if bad:
        problems.append(f"{bad} malformed scene(s) (missing {'/'.join(req_keys)})")

    # Scene numbering: contiguous 1..N is what the pipeline assumes downstream.
    nums = [el.get("scene") for el in scenes if isinstance(el, dict)]
    numbering_ok = nums == list(range(1, len(nums) + 1))
    if nums and not numbering_ok:
        problems.append("scene numbers not contiguous from 1")

    n_scenes = len(scenes)
    want_scenes = int(exp.get("scene_count") or 0)
    scene_tol = int(exp.get("scene_count_tolerance") or 0)
    scene_delta = n_scenes - want_scenes if want_scenes else 0
    scene_ok = (abs(scene_delta) <= scene_tol) if want_scenes else True
    if want_scenes and not scene_ok:
        problems.append(f"scene count {n_scenes} vs requested {want_scenes} "
                        f"(tolerance ±{scene_tol})")

    narration = " ".join(narr_parts)
    n_words = len(_words(narration))
    budget = int(exp.get("word_budget") or 0)
    is_ceiling = bool(exp.get("word_budget_is_ceiling"))
    tol_pct = float(exp.get("word_tolerance_pct") or 10)
    word_pct = ((n_words - budget) / budget * 100.0) if budget else 0.0
    over_budget = bool(budget) and n_words > budget
    if budget:
        if is_ceiling and over_budget:
            # Production rejects this outright — it means the finished video would run
            # longer than the source.
            problems.append(f"OVER the hard word CEILING by {word_pct:+.0f}% "
                            f"({n_words} > {budget}) — production would reject this")
        elif abs(word_pct) > tol_pct:
            problems.append(f"word count {n_words} vs budget {budget} ({word_pct:+.0f}%, "
                            f"tolerance ±{tol_pct:.0f}%)")

    # Pace: what the narration would actually run to at the pipeline's assumed pace,
    # against the duration the prompt asked for.
    target_sec = float(exp.get("target_duration_sec") or 0)
    est_sec = n_words / VI_WORDS_PER_SEC if VI_WORDS_PER_SEC else 0.0
    pace_wps = (n_words / target_sec) if target_sec else 0.0
    dur_ratio = (est_sec / target_sec) if target_sec else 0.0

    vn = _vn_share(narration)
    if narration and vn < 0.55:
        problems.append(f"narration only ~{vn:.0%} Vietnamese-looking — "
                        "possible English output")

    # Footage-shape extra: source ranges must lie inside the prompt's window.
    range_violations = 0
    if task.shape == "footage":
        win = exp.get("source_window") or [0, target_sec or 0]
        lo, hi = float(win[0]), float(win[1])
        for el in scenes:
            if not isinstance(el, dict):
                continue
            try:
                a, b = float(el.get("sourceStart")), float(el.get("sourceEnd"))
            except (TypeError, ValueError):
                range_violations += 1
                continue
            if a < lo - 0.01 or b > hi + 0.01 or b <= a:
                range_violations += 1
        if range_violations:
            problems.append(f"{range_violations} scene(s) with source range outside "
                            f"{lo:.0f}-{hi:.0f}s or inverted")

    # Verdict. FAIL = unusable by the pipeline as-is. WARN = usable but off-spec.
    hard_fail = (not shape_ok) or (is_ceiling and over_budget) or (narration and vn < 0.55)
    verdict = "FAIL" if hard_fail else ("WARN" if problems else "OK")

    return {
        "scenes": n_scenes, "want_scenes": want_scenes, "scene_delta": scene_delta,
        "scene_ok": scene_ok, "shape_ok": shape_ok, "malformed": bad,
        "numbering_ok": numbering_ok,
        "words": n_words, "word_budget": budget, "word_pct": round(word_pct, 1),
        "word_budget_is_ceiling": is_ceiling, "over_budget": over_budget,
        "est_narration_sec": round(est_sec, 1), "target_duration_sec": target_sec,
        "duration_ratio": round(dur_ratio, 2), "pace_words_per_sec": round(pace_wps, 2),
        "vn_share": round(vn, 3), "range_violations": range_violations,
        "problems": problems, "verdict": verdict,
    }


# ==================================================================================
# OpenRouter live discovery — free + structured-output-capable + long-enough context
# ==================================================================================

def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def discover_openrouter_free(client: httpx.Client, min_context: int,
                             probe_limit: int, verbose: bool = True) -> list[dict]:
    """List OpenRouter's zero-cost models and read each one's FREE ENDPOINT capabilities.

    Why per-endpoint: structured-output support and context length are properties of the
    endpoint, not the model. Measured 2026-07-30 — google/gemma-4-26b-a4b-it:free had two
    free endpoints, one with structured_outputs=true at 131k ctx and one with
    structured_outputs=FALSE at 262k ctx. The models-list headline numbers describe the
    BEST endpoint, so trusting them picks a config you may never actually get.

    Both /models and /models/<id>/endpoints are unauthenticated (verified), so this runs
    with no key and does not consume the 50-RPD free-model quota.
    """
    r = client.get(f"{OPENROUTER_API}/models", timeout=60)
    r.raise_for_status()
    models = (r.json() or {}).get("data") or []

    free_ids: list[dict] = []
    for m in models:
        pr = m.get("pricing") or {}
        if _num(pr.get("prompt"), -1) == 0.0 and _num(pr.get("completion"), -1) == 0.0:
            free_ids.append(m)
    # Prefer the explicit :free variants first, then any other zero-priced model.
    free_ids.sort(key=lambda m: (0 if str(m.get("id", "")).endswith(":free") else 1,
                                 -_num((m.get("top_provider") or {}).get("context_length"))))

    if verbose:
        print(f"OpenRouter: {len(models)} models total, {len(free_ids)} at $0 "
              f"(probing endpoints for the first {min(probe_limit, len(free_ids))})")

    rows: list[dict] = []
    for m in free_ids[:probe_limit]:
        mid = m.get("id") or ""
        row = {"id": mid, "headline_ctx": int(_num((m.get("top_provider") or {})
                                                  .get("context_length"))),
               "endpoints": [], "error": None}
        try:
            er = client.get(f"{OPENROUTER_API}/models/{mid}/endpoints", timeout=45)
            if er.status_code != 200:
                row["error"] = f"HTTP {er.status_code}"
            else:
                data = (er.json() or {}).get("data") or {}
                for ep in data.get("endpoints") or []:
                    epr = ep.get("pricing") or {}
                    if not (_num(epr.get("prompt"), -1) == 0.0
                            and _num(epr.get("completion"), -1) == 0.0):
                        continue  # a PAID endpoint of a model that also has a free one
                    sp = ep.get("supported_parameters") or []
                    row["endpoints"].append({
                        "provider": ep.get("provider_name") or "?",
                        "ctx": int(_num(ep.get("context_length"))),
                        "max_out": int(_num(ep.get("max_completion_tokens"))),
                        "structured_outputs": "structured_outputs" in sp,
                        "response_format": "response_format" in sp,
                        "status": ep.get("status"),
                        "uptime_1d": _num(ep.get("uptime_last_1d")),
                    })
        except httpx.HTTPError as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"

        eps = row["endpoints"]
        # Usable = at least one FREE endpoint that can do strict structured output AND has
        # enough context for a long transcript. The context we judge is the endpoint's own.
        good = [e for e in eps if e["structured_outputs"] and e["ctx"] >= min_context]
        row["usable"] = bool(good)
        row["best_ctx"] = max((e["ctx"] for e in eps), default=0)
        row["any_structured"] = any(e["structured_outputs"] for e in eps)
        row["any_response_format"] = any(e["response_format"] for e in eps)
        row["best_uptime_1d"] = max((e["uptime_1d"] for e in eps), default=0.0)
        # Rank usable candidates by uptime then context — reliability first, because a
        # free endpoint at 93.7% daily uptime fails roughly 1 request in 16.
        row["rank"] = (max((e["uptime_1d"] for e in good), default=0.0),
                       max((e["ctx"] for e in good), default=0))
        rows.append(row)
        time.sleep(0.15)  # be polite to an unauthenticated public endpoint
    return rows


def print_discovery(rows: list[dict], min_context: int) -> None:
    print(f"\n=== OpenRouter $0 models — endpoint capabilities "
          f"(min usable context {min_context:,}) ===")
    hdr = (f"  {'MODEL ID':<46} {'FREE EP':<20} {'CTX':>9} {'MAX OUT':>8} "
           f"{'STRUCT':<6} {'RESP_FMT':<8} {'UP 1d':>7} USABLE")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in sorted(rows, key=lambda x: (not x["usable"], -x["rank"][0], -x["best_ctx"])):
        if r["error"]:
            print(f"  {r['id'][:46]:<46} {'(endpoint probe failed: ' + r['error'] + ')'}")
            continue
        if not r["endpoints"]:
            print(f"  {r['id'][:46]:<46} {'(no free endpoint)':<20}")
            continue
        for i, e in enumerate(r["endpoints"]):
            usable = "yes" if (e["structured_outputs"] and e["ctx"] >= min_context) else "no"
            print(f"  {(r['id'] if i == 0 else '')[:46]:<46} {e['provider'][:20]:<20} "
                  f"{e['ctx']:>9,} {(e['max_out'] or 0):>8,} "
                  f"{('yes' if e['structured_outputs'] else 'NO'):<6} "
                  f"{('yes' if e['response_format'] else 'no'):<8} "
                  f"{e['uptime_1d']:>6.1f}% {usable}")
    usable = [r for r in rows if r["usable"]]
    print(f"\n  {len(usable)} of {len(rows)} probed model(s) have a free endpoint that is "
          f"structured-output capable AND >= {min_context:,} context.")
    if usable:
        best = max(usable, key=lambda r: r["rank"])
        print(f"  Bench default would be: {best['id']} "
              f"(uptime {best['rank'][0]:.1f}%, ctx {best['rank'][1]:,})")
    print("  NOTE: free model IDs churn weekly — this table is read LIVE, never hardcoded.\n")


def pick_openrouter_model(client: httpx.Client, min_context: int,
                          probe_limit: int) -> tuple[str | None, str]:
    """Choose the best free OpenRouter model for the bench, live. Returns (id, why)."""
    try:
        rows = discover_openrouter_free(client, min_context, probe_limit, verbose=False)
    except httpx.HTTPError as exc:
        return None, f"discovery failed ({type(exc).__name__}: {exc})"
    usable = [r for r in rows if r["usable"]]
    if not usable:
        loose = [r for r in rows if r["any_structured"]]
        if loose:
            b = max(loose, key=lambda r: r["best_ctx"])
            return b["id"], (f"no free endpoint reached {min_context:,} context; using the "
                             f"best structured-output one ({b['best_ctx']:,} ctx)")
        return None, "no $0 model has a structured-output-capable free endpoint right now"
    best = max(usable, key=lambda r: r["rank"])
    return best["id"], (f"live pick: uptime {best['rank'][0]:.1f}%/1d, "
                        f"endpoint ctx {best['rank'][1]:,}")


# ==================================================================================
# One request
# ==================================================================================

@dataclass
class Leg:
    provider: str
    model: str
    task: str
    repeat: int
    status: int | None = None
    error: str | None = None
    skipped: str | None = None
    latency: float = 0.0
    raw: str = ""
    served_model: str | None = None
    usage: dict = field(default_factory=dict)
    structured: str = "json_schema"   # json_schema | fallback:none | none
    parse_error: str | None = None
    metrics: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return f"{self.provider}#{self.repeat}"

    @property
    def verdict(self) -> str:
        if self.skipped:
            return "SKIP"
        if self.error or self.status != 200:
            return "ERROR"
        if self.parse_error:
            return "FAIL"
        return self.metrics.get("verdict") or "FAIL"


def _build_body(prov: Provider, model: str, task: Task, structured: bool) -> dict:
    msgs = []
    if task.system_prompt:
        msgs.append({"role": "system", "content": task.system_prompt})
    user = task.prompt
    if structured and task.schema:
        # OpenAI-style strict structured output requires an OBJECT root, but the pipeline's
        # prompt asks for a bare array. Tell the model about the wrapper so the two agree;
        # extract_scene_list() unwraps it. Without this note a model can obey the prompt
        # (bare array) and violate the schema, or vice versa.
        user += ('\n\nSTRUCTURED OUTPUT: return the array as the value of a single '
                 'top-level "scenes" key, i.e. {"scenes": [ ...the array... ]}.')
    msgs.append({"role": "user", "content": user})

    body: dict = {
        "model": model,
        "messages": msgs,
        "temperature": float(os.getenv("LLM_BENCH_TEMPERATURE", "0.7")),
        "max_tokens": task.max_tokens + prov.max_tokens_bonus,
        "stream": False,
    }
    if structured and task.schema:
        body["response_format"] = {"type": "json_schema", "json_schema": task.schema}
    body.update(prov.extra_body)
    return body


_RESP_FMT_ERR = re.compile(r"(?i)response_format|json_schema|structured|schema")


def call_leg(client: httpx.Client, prov: Provider, model: str, task: Task,
             repeat: int, timeout: int, structured: bool) -> Leg:
    """One request. Never raises for a provider-side problem — it is measured and returned."""
    leg = Leg(provider=prov.key, model=model, task=task.id, repeat=repeat)
    headers = {"Content-Type": "application/json"}
    key = prov.api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    headers.update(prov.extra_headers)

    body = _build_body(prov, model, task, structured)
    leg.structured = "json_schema" if body.get("response_format") else "none"
    try:
        assert_no_secrets(json.dumps(body.get("messages"), ensure_ascii=False),
                          f"{prov.key}/{task.id} prompt")
    except ValueError as exc:
        leg.error = str(exc)
        return leg

    url = f"{prov.base_url.rstrip('/')}/chat/completions"
    t0 = time.time()
    try:
        r = client.post(url, headers=headers, json=body, timeout=timeout)
        leg.status = r.status_code
        # A provider that cannot do json_schema at all (z.ai is UNVERIFIED here): retry
        # ONCE without response_format, and record that it had to. This measures the
        # capability instead of scoring the leg as a blanket failure — and it is NOT a
        # fallback to a paid path, it is the same $0 endpoint, prompt-only JSON.
        if r.status_code == 400 and body.get("response_format") \
                and _RESP_FMT_ERR.search(r.text or ""):
            body.pop("response_format", None)
            leg.structured = "fallback:none"
            r = client.post(url, headers=headers, json=body, timeout=timeout)
            leg.status = r.status_code
        leg.latency = round(time.time() - t0, 2)

        if r.status_code != 200:
            snippet = (r.text or "").strip().replace("\n", " ")[:400]
            leg.error = f"HTTP {r.status_code}: {snippet}"
            if r.status_code == 429:
                for h in ("retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"):
                    if h in r.headers:
                        leg.error += f" | {h}={r.headers[h]}"
            return leg

        data = r.json()
        leg.served_model = data.get("model")
        leg.usage = data.get("usage") or {}
        choices = data.get("choices") or []
        msg = (choices[0].get("message") or {}) if choices else {}
        leg.raw = msg.get("content") or ""
        if not leg.raw and choices:
            # Some gateways put a refusal / reasoning-only message here.
            leg.raw = json.dumps(choices[0], ensure_ascii=False)
        finish = choices[0].get("finish_reason") if choices else None
        if finish and finish not in ("stop", "STOP"):
            leg.error = f"finish_reason={finish}"  # e.g. 'length' = truncated output
    except httpx.TimeoutException:
        leg.latency = round(time.time() - t0, 2)
        leg.error = f"timeout after {timeout}s"
        return leg
    except httpx.HTTPError as exc:
        leg.latency = round(time.time() - t0, 2)
        leg.error = f"{type(exc).__name__}: {exc}"
        return leg
    except (ValueError, KeyError, IndexError) as exc:
        leg.latency = round(time.time() - t0, 2)
        leg.error = f"malformed response envelope: {exc}"
        return leg

    try:
        scenes = extract_scene_list(leg.raw)
    except ValueError as exc:
        leg.parse_error = str(exc)
        return leg
    leg.metrics = score(scenes, task)
    leg.metrics["parsed_scenes"] = scenes  # popped before the meta file is written
    return leg


# ==================================================================================
# Artifacts
# ==================================================================================

def write_artifacts(out_dir: str, leg: Leg, task: Task) -> None:
    os.makedirs(out_dir, exist_ok=True)
    stem = f"{task.id}__{leg.provider}__r{leg.repeat}"
    scenes = leg.metrics.pop("parsed_scenes", None)

    # The point of the whole exercise: the owner READS this file and judges the Vietnamese.
    raw_p = os.path.join(out_dir, stem + ".raw.txt")
    Path(raw_p).write_text(leg.raw or "(no content)", encoding="utf-8")
    leg.artifacts["raw"] = raw_p

    if scenes is not None:
        # Narration-only view, so fluency can be read without JSON noise.
        lines = []
        for el in scenes:
            if isinstance(el, dict) and isinstance(el.get("narration"), str):
                lines.append(f"[{el.get('scene')}] {el['narration']}")
        nar_p = os.path.join(out_dir, stem + ".narration.txt")
        Path(nar_p).write_text("\n".join(lines) + "\n", encoding="utf-8")
        leg.artifacts["narration"] = nar_p

        json_p = os.path.join(out_dir, stem + ".scenes.json")
        Path(json_p).write_text(json.dumps(scenes, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        leg.artifacts["scenes"] = json_p

    meta_p = os.path.join(out_dir, stem + ".meta.json")
    Path(meta_p).write_text(json.dumps({
        "provider": leg.provider, "model_requested": leg.model,
        "model_served": leg.served_model, "task": leg.task, "repeat": leg.repeat,
        "http_status": leg.status, "error": leg.error, "skipped": leg.skipped,
        "latency_sec": leg.latency, "structured_output": leg.structured,
        "usage": leg.usage, "parse_error": leg.parse_error,
        "metrics": leg.metrics, "verdict": leg.verdict,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    leg.artifacts["meta"] = meta_p


# ==================================================================================
# Reporting
# ==================================================================================

def _fmt_pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.0f}%"


def render_table(task: Task, legs: list[Leg]) -> str:
    exp = task.expect
    out: list[str] = []
    out.append(f"\n=== TASK {task.id} — {task.label} ===")
    out.append(f"  requested: {exp.get('scene_count')} scenes, "
               f"{exp.get('word_budget')} words"
               f"{' (HARD CEILING)' if exp.get('word_budget_is_ceiling') else ''}, "
               f"{exp.get('target_duration_sec')}s target, shape={task.shape}")
    hdr = (f"  {'PROVIDER':<12} {'MODEL':<30} {'HTTP':>5} {'LAT s':>7} {'JSON':<5} "
           f"{'SHAPE':<5} {'SCN':>7} {'WORDS':>11} {'EST s':>7} {'w/s':>5} "
           f"{'VN%':>4} {'STRUCT':<12} VERDICT")
    out.append(hdr)
    out.append("  " + "-" * (len(hdr) - 2))
    for lg in legs:
        m = lg.metrics
        if lg.skipped:
            dash = "-"
            out.append(f"  {lg.provider:<12} {dash:<30} {dash:>5} {dash:>7} {dash:<5} "
                       f"{dash:<5} {dash:>7} {dash:>11} {dash:>7} {dash:>5} {dash:>4} "
                       f"{dash:<12} SKIP — {lg.skipped}")
            continue
        # Precompute every cell: keeps the format string readable and avoids nested
        # quoting inside f-strings.
        model = (lg.served_model or lg.model or "?")[:30]
        http = str(lg.status) if lg.status else "-"
        json_ok = "ok" if m else "FAIL"
        shape = ("ok" if m.get("shape_ok") else "BAD") if m else "-"
        scn = f"{m.get('scenes')}/{m.get('want_scenes')}" if m else "-"
        wrd = f"{m.get('words')} {_fmt_pct(m.get('word_pct'))}" if m else "-"
        est = f"{m.get('est_narration_sec', 0):.0f}" if m else "-"
        wps = f"{m.get('pace_words_per_sec', 0):.1f}" if m else "-"
        vnp = f"{m.get('vn_share', 0) * 100:.0f}" if m else "-"
        out.append(f"  {lg.provider:<12} {model:<30} {http:>5} {lg.latency:>7.1f} "
                   f"{json_ok:<5} {shape:<5} {scn:>7} {wrd:>11} {est:>7} {wps:>5} "
                   f"{vnp:>4} {lg.structured:<12} {lg.verdict}")
        detail: list[str] = []
        if lg.error:
            detail.append(lg.error[:300])
        if lg.parse_error:
            detail.append("parse: " + lg.parse_error)
        detail.extend(m.get("problems") or [])
        for d in detail:
            out.append(f"      ! {d}")
    return "\n".join(out)


LEGEND = """
HOW TO READ IT
  HTTP      response status. 429 = rate limited (see the daily-cap note below).
  LAT s     wall-clock seconds for the one request. Free endpoints are slow under load.
  JSON      did the body parse into a scene array at all (after fence/wrapper tolerance).
  SHAPE     does every scene carry the keys the pipeline needs.
  SCN       scenes returned / scenes requested.
  WORDS     total Vietnamese narration words, and % off the prompt's word budget.
  EST s     narration seconds that word count implies at the pipeline's pace
            ({WPS} words/sec) — compare it against the task's target duration.
  w/s       words per second of TARGET duration. Well under the pace = the model
            under-filled the video; well over = it would overrun the source.
  VN%       HEURISTIC share of narration tokens that look Vietnamese. Below ~55%
            is treated as "answered in English" and hard-fails. Around 75-95% is
            normal for good output that keeps English proper nouns.
  STRUCT    json_schema = strict structured output was accepted.
            fallback:none = the provider rejected response_format, so the request was
            re-sent prompt-only (still the same $0 endpoint). none = not requested.
  VERDICT   OK / WARN (usable but off-spec) / FAIL (unusable as-is) / ERROR / SKIP.

WHAT THIS TOOL DOES NOT MEASURE
  Fluency, register, and faithfulness to the source. No metric here can judge those.
  Read the *.narration.txt artifacts and rank them yourself — that is the whole point.
""".replace("{WPS}", f"{VI_WORDS_PER_SEC:.1f}")  # plain replace: the text is full of literal %


# ==================================================================================
# Rate limiting
# ==================================================================================

class Pacer:
    """Global sequential pacer. Free tiers are per-account and governed globally, so one
    shared minimum interval across all legs is the correct model."""

    def __init__(self, rpm: int):
        self.min_gap = (60.0 / rpm) if rpm > 0 else 0.0
        self.last = 0.0

    def wait(self) -> None:
        if self.min_gap <= 0:
            return
        gap = time.time() - self.last
        if gap < self.min_gap:
            time.sleep(self.min_gap - gap)
        self.last = time.time()


class RateLimitAbort(Exception):
    pass


# ==================================================================================
# Model resolution per provider
# ==================================================================================

def resolve_models(client: httpx.Client, wanted: list[str], overrides: dict[str, str],
                   min_context: int, probe_limit: int) -> dict[str, tuple[str | None, str]]:
    """(model, note_or_skip_reason) per provider. `model is None` => skip the leg."""
    out: dict[str, tuple[str | None, str]] = {}
    for pk in wanted:
        prov = PROVIDERS[pk]
        if prov.needs_key and not prov.api_key():
            envs = " or ".join(prov.key_envs)
            out[pk] = (None, f"no key — set {envs} (free key: {prov.signup_url})")
            continue
        if pk in overrides:
            out[pk] = (overrides[pk], "model from --models")
            continue
        if pk == "openrouter" and not prov.default_model:
            mid, why = pick_openrouter_model(client, min_context, probe_limit)
            out[pk] = (mid, why) if mid else (None, why)
            continue
        if pk == "ollama":
            base = prov.base_url.rsplit("/v1", 1)[0]
            try:
                r = client.get(f"{base}/api/tags", timeout=3)
                r.raise_for_status()
                tags = [t.get("name") for t in (r.json() or {}).get("models") or []]
            except httpx.HTTPError as exc:
                out[pk] = (None, f"daemon not reachable at {base} ({type(exc).__name__})")
                continue
            if prov.default_model:
                out[pk] = (prov.default_model, "model from LLM_BENCH_OLLAMA_MODEL")
            elif tags:
                out[pk] = (tags[0], f"first installed model of {len(tags)}")
            else:
                out[pk] = (None, "daemon up but no models installed (`ollama pull ...`)")
            continue
        out[pk] = (prov.default_model, "provider default")
    return out


# ==================================================================================
# Dry run — parsing/scoring only, no network, no keys
# ==================================================================================

def run_dry(tasks: list[Task]) -> int:
    """Score the saved fixture responses. This is the keyless verification path: it
    exercises extract_scene_list() + score() + the table renderer end to end."""
    by_id = {t.id: t for t in tasks}
    if not os.path.isdir(_RESPONSE_DIR):
        print(f"no fixture responses at {_RESPONSE_DIR}")
        return 1
    files = sorted(fn for fn in os.listdir(_RESPONSE_DIR) if fn.endswith(".json"))
    if not files:
        print(f"no fixture responses in {_RESPONSE_DIR}")
        return 1

    print("DRY RUN — no network, no keys. Scoring saved fixture responses to verify "
          "parsing/scoring.")
    per_task: dict[str, list[Leg]] = {}
    mismatches = 0
    for fn in files:
        env = json.loads(Path(os.path.join(_RESPONSE_DIR, fn)).read_text(encoding="utf-8"))
        bench = env.get("_bench") or {}
        tid = bench.get("task")
        if tid not in by_id:
            print(f"  ! {fn}: names unknown task '{tid}' — skipped")
            continue
        task = by_id[tid]
        leg = Leg(provider=bench.get("as_provider") or "fixture",
                  model=env.get("model") or "(fixture)", task=tid,
                  repeat=1, status=200,
                  latency=float(bench.get("latency_sec") or 0.0),
                  structured=bench.get("structured") or "json_schema")
        leg.served_model = env.get("model")
        leg.usage = env.get("usage") or {}
        choices = env.get("choices") or []
        leg.raw = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
        try:
            scenes = extract_scene_list(leg.raw)
            leg.metrics = score(scenes, task)
            leg.metrics.pop("parsed_scenes", None)
        except ValueError as exc:
            leg.parse_error = str(exc)
        per_task.setdefault(tid, []).append(leg)
        exp = bench.get("expect_verdict")
        got = leg.verdict
        mark = ""
        if exp:
            ok = (exp == got)
            mismatches += 0 if ok else 1
            mark = f"  [expected {exp} -> {'MATCH' if ok else 'MISMATCH'}]"
        print(f"  {fn:<56} -> {got}{mark}")

    for tid, legs in per_task.items():
        print(render_table(by_id[tid], legs))
    print(LEGEND)
    if mismatches:
        print(f"DRY RUN: {mismatches} fixture(s) did not produce the expected verdict — "
              "the scorer changed behaviour, investigate before trusting a live run.")
        return 1
    print("DRY RUN OK — every fixture produced its expected verdict.")
    return 0


# ==================================================================================
# Live run
# ==================================================================================

def run_bench(args, tasks: list[Task]) -> int:
    wanted = [p.strip() for p in (args.providers or ",".join(PROVIDERS)).split(",") if p.strip()]
    unknown = [p for p in wanted if p not in PROVIDERS]
    if unknown:
        raise SystemExit(f"unknown provider(s): {', '.join(unknown)} "
                         f"(known: {', '.join(PROVIDERS)})")
    overrides: dict[str, str] = {}
    for pair in (args.models or "").split(","):
        if "=" in pair:
            k, _, v = pair.partition("=")
            k, v = k.strip(), v.strip()
            if k not in PROVIDERS:
                raise SystemExit(f"--models: unknown provider '{k}'")
            overrides[k] = v

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out_dir = os.path.join(args.out_dir or DEFAULT_OUT_ROOT, stamp)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"cannot create output dir {out_dir}: {exc}\n"
                         f"pass --out-dir <writable path> (artifacts must stay out of the repo)")

    lines: list[str] = []

    def emit(s: str = "") -> None:
        print(s)
        lines.append(s)

    with httpx.Client(follow_redirects=True) as client:
        emit("Resolving models ...")
        resolved = resolve_models(client, wanted, overrides, args.min_context,
                                  args.discover_limit)
        for pk in wanted:
            model, note = resolved[pk]
            emit(f"  {PROVIDERS[pk].label:<28} "
                 f"{(model or 'SKIP'):<40} ({note})")

        pacer = Pacer(args.rpm)
        consecutive_429 = 0
        results: dict[str, list[Leg]] = {t.id: [] for t in tasks}
        aborted = False

        try:
            for rep in range(1, args.repeat + 1):
                for task in tasks:
                    for pk in wanted:
                        prov = PROVIDERS[pk]
                        model, note = resolved[pk]
                        if not model:
                            lg = Leg(provider=pk, model="", task=task.id, repeat=rep)
                            lg.skipped = note
                            results[task.id].append(lg)
                            continue
                        print(f"  -> {pk} / {task.id} / r{rep} ...", flush=True)
                        pacer.wait()
                        lg = call_leg(client, prov, model, task, rep, args.timeout,
                                      structured=not args.no_structured)
                        write_artifacts(out_dir, lg, task)
                        results[task.id].append(lg)
                        if lg.status == 429:
                            consecutive_429 += 1
                            if consecutive_429 >= args.max_429:
                                raise RateLimitAbort(
                                    f"{consecutive_429} consecutive 429s")
                            # Back off progressively before the next request.
                            time.sleep(min(60, 5 * consecutive_429))
                        else:
                            consecutive_429 = 0
        except RateLimitAbort as exc:
            aborted = True
            emit("")
            emit(f"ABORTED: {exc}. STOPPING THE WHOLE RUN ON PURPOSE.")
            emit("  OpenRouter's $0 tier is 20 requests/MINUTE and only 50 requests/DAY "
                 "(UTC-day reset).")
            emit("  A retry loop would burn the remaining daily quota in under a minute, "
                 "so nothing further is attempted.")
            emit("  Check which leg 429'd in the table below, wait for the reset, then "
                 "re-run with fewer --repeat or --providers.")
            emit("  Do NOT 'fix' this by buying credits — this project is $0-only.")
        except KeyboardInterrupt:
            aborted = True
            emit("\nInterrupted — partial results below.")

    for task in tasks:
        legs = results[task.id]
        if legs:
            emit(render_table(task, legs))
    emit(LEGEND)

    counts: dict[str, int] = {}
    for legs in results.values():
        for lg in legs:
            counts[lg.verdict] = counts.get(lg.verdict, 0) + 1
    emit("RUN SUMMARY: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    emit(f"Artifacts: {out_dir}")
    emit("  *.raw.txt        exactly what the model returned")
    emit("  *.narration.txt  narration lines only — READ THIS to judge the Vietnamese")
    emit("  *.scenes.json    parsed scene array")
    emit("  *.meta.json      status / latency / usage / metrics for that leg")
    skipped = [lg for legs in results.values() for lg in legs if lg.skipped]
    if skipped:
        emit("")
        emit("NOT RUN (reported as-is, no numbers invented):")
        seen = set()
        for lg in skipped:
            if lg.provider in seen:
                continue
            seen.add(lg.provider)
            emit(f"  {lg.provider:<12} {lg.skipped}")

    Path(os.path.join(out_dir, "summary.txt")).write_text("\n".join(lines) + "\n",
                                                          encoding="utf-8")
    Path(os.path.join(out_dir, "summary.json")).write_text(json.dumps({
        "run": stamp,
        "words_per_sec_assumed": VI_WORDS_PER_SEC,
        "rpm": args.rpm, "repeat": args.repeat,
        "structured_requested": not args.no_structured,
        "aborted_on_rate_limit": aborted,
        "resolved": {k: {"model": v[0], "note": v[1]} for k, v in resolved.items()},
        "legs": [{
            "provider": lg.provider, "task": lg.task, "repeat": lg.repeat,
            "model_requested": lg.model, "model_served": lg.served_model,
            "http_status": lg.status, "latency_sec": lg.latency,
            "structured_output": lg.structured, "usage": lg.usage,
            "error": lg.error, "skipped": lg.skipped, "parse_error": lg.parse_error,
            "verdict": lg.verdict, "metrics": lg.metrics,
            "artifacts": lg.artifacts,
        } for legs in results.values() for lg in legs],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if aborted:
        return 4
    return 0 if not counts.get("ERROR") and not counts.get("FAIL") else 1


# ==================================================================================
# main
# ==================================================================================

def main() -> int:
    ap = argparse.ArgumentParser(
        description="A/B free-tier LLMs on one fixed Vietnamese script-gen task.",
        epilog="Env vars and $0 key signup URLs: tools/README-llm-bench.md")
    ap.add_argument("--list-models", action="store_true",
                    help="Live OpenRouter $0 catalogue + per-ENDPOINT capabilities. "
                         "Needs no key (the /models endpoints are public).")
    ap.add_argument("--dry-run", action="store_true",
                    help="No network, no keys: score the saved fixture responses to "
                         "verify parsing/scoring.")
    ap.add_argument("--providers", default=None,
                    help=f"comma list; default all: {','.join(PROVIDERS)}")
    ap.add_argument("--tasks", default=None,
                    help="comma list of task ids (default: all fixture tasks)")
    ap.add_argument("--models", default=None,
                    help="override a provider's model: gemini=gemini-2.5-flash,"
                         "openrouter=openai/gpt-oss-20b:free")
    ap.add_argument("--repeat", type=int, default=1,
                    help="samples per leg (free models vary run to run; 3 is a good "
                         "sample without eating the daily cap)")
    ap.add_argument("--rpm", type=int, default=20,
                    help="global requests/minute ceiling (default 20 = OpenRouter free)")
    ap.add_argument("--max-429", type=int, default=3,
                    help="consecutive 429s that abort the whole run (default 3)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                    help=f"per-request seconds (default {DEFAULT_TIMEOUT})")
    ap.add_argument("--min-context", type=int, default=32768,
                    help="minimum ENDPOINT context to call a free model usable "
                         "(default 32768)")
    ap.add_argument("--discover-limit", type=int, default=25,
                    help="how many $0 models to probe for endpoint detail (default 25)")
    ap.add_argument("--no-structured", action="store_true",
                    help="do not send response_format — measure prompt-only JSON")
    ap.add_argument("--out-dir", default=None,
                    help=f"artifact root (default {DEFAULT_OUT_ROOT}; keep it OUT of the repo)")
    ap.add_argument("--prompt-file", default=None,
                    help="bench a REAL prompt captured from a past job instead of the "
                         "fixture tasks")
    ap.add_argument("--shape", default="footage", choices=("footage", "topic"),
                    help="--prompt-file only: expected scene shape (default footage)")
    ap.add_argument("--expect-scenes", type=int, default=40, help="--prompt-file only")
    ap.add_argument("--expect-words", type=int, default=1161, help="--prompt-file only")
    ap.add_argument("--expect-duration", type=int, default=600, help="--prompt-file only")
    args = ap.parse_args()

    if args.list_models:
        with httpx.Client(follow_redirects=True) as client:
            try:
                rows = discover_openrouter_free(client, args.min_context,
                                                args.discover_limit)
            except httpx.HTTPError as exc:
                print(f"OpenRouter model discovery failed: {type(exc).__name__}: {exc}")
                return 2
        print_discovery(rows, args.min_context)
        return 0

    only = [t.strip() for t in (args.tasks or "").split(",") if t.strip()] or None
    if args.prompt_file:
        if not os.path.isfile(args.prompt_file):
            raise SystemExit(f"--prompt-file not found: {args.prompt_file}")
        tasks = [task_from_prompt_file(args.prompt_file, args.shape, args.expect_scenes,
                                       args.expect_words, args.expect_duration)]
    else:
        tasks = load_tasks(only)
    if not tasks:
        raise SystemExit("no tasks selected")

    if args.dry_run:
        return run_dry(load_tasks(None))
    return run_bench(args, tasks)


if __name__ == "__main__":
    sys.exit(main())
