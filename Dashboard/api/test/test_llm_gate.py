"""Tests for the multi-provider LLM gate (llm_gate.py) and its wiring into generate.py.

THE POINT OF THIS FILE is the first section: proving that a job which picks NO provider
behaves EXACTLY as it did before the gate existed. Everything else is secondary.

  1. DEFAULT-PATH EQUIVALENCE
     - the `claude -p` argv is byte-for-byte the pre-gate argv;
     - the script disk-cache key hashes to the pre-gate value (a changed key would
       silently invalidate 24h of cached batches AND is the exact bug class the TTS cache
       already hit — see the tts-cache-key-omits-engine-flags memory note);
     - `_llm_kwargs` yields {} so the call shape into `_run_claude_script` /
       `_run_batches_parallel` is unchanged (which is why every pre-existing monkeypatch
       stub in this suite still matches).

  2. PROVIDER ROUTING — gemini/openrouter take the httpx leg and never spawn a process;
     the cache key differs per provider/model; a missing API key FAILS instead of quietly
     falling back to claude-cli.

  3. ERROR TAXONOMY — 429 and transient 5xx are retryable, 400/401/404 are not, and
     `generate._is_retryable` honors the LLMError flag without re-judging its status code.

NO NETWORK: every HTTP call is stubbed at httpx.Client, every subprocess at
subprocess.Popen. Nothing here consumes a free-tier request or a subscription call.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_llm_gate.py -q
"""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate  # noqa: E402
import llm_gate  # noqa: E402
from fastapi import HTTPException  # noqa: E402


_VALID_SCENES = [
    {"scene": 1, "narration": "Xin chào", "image_prompt": "a wide cinematic shot"},
    {"scene": 2, "narration": "Tiếp theo", "image_prompt": "a close-up shot"},
]


# --------------------------------------------------------------------------- #
# Doubles                                                                      #
# --------------------------------------------------------------------------- #
def _stream_lines(result_text):
    """The newline-delimited stream-json events generate._read_stream_json_result parses."""
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "result", "subtype": "success", "is_error": False, "result": result_text},
    ]
    return [json.dumps(e) + "\n" for e in events]


class _GoodProc:
    """A fake Popen streaming a valid result event."""

    def __init__(self, result_text):
        self.pid = 5555
        self.returncode = 0
        self.stdout = iter(_stream_lines(result_text))

    def communicate(self, timeout=None):
        return ("", "")

    def wait(self, timeout=None):
        return self.returncode


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client. Records every POST so the request SHAPE can be asserted
    (auth header, model, provider block) without any network."""

    posts: list = []

    def __init__(self, response=None, **_kw):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, headers=None, json=None, timeout=None):
        type(self).posts.append({"url": url, "headers": headers or {}, "body": json or {},
                                 "timeout": timeout})
        return self._response


def _chat_envelope(content, model="gemini-flash-latest", finish="stop"):
    return {"model": model, "usage": {"total_tokens": 1},
            "choices": [{"finish_reason": finish, "message": {"content": content}}]}


@pytest.fixture(autouse=True)
def _reset_fake_posts():
    _FakeClient.posts = []
    yield
    _FakeClient.posts = []


# =========================================================================== #
# 1. DEFAULT-PATH EQUIVALENCE — the invariant this whole phase rests on       #
# =========================================================================== #

def test_default_provider_argv_matches_pre_gate(monkeypatch):
    """provider unset -> the EXACT `claude -p` argv the code built before the gate.

    The literal below is the pre-gate argument list copied verbatim out of
    `_run_claude_script_once`. The only edit that touched it was
    `SCRIPT_GEN_MODEL` -> `(model or SCRIPT_GEN_MODEL)`; this asserts that substitution
    is a genuine no-op on the default path (llm_gate.resolve resolves the model to
    SCRIPT_GEN_MODEL itself, so it is not even None by the time it arrives)."""
    captured = {}

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _GoodProc(json.dumps(_VALID_SCENES))

    monkeypatch.setattr(generate.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(generate, "_kill_proc_tree", lambda p: None)

    scenes = generate._run_claude_script("PROMPT-TEXT", timeout=30)
    assert scenes == _VALID_SCENES

    expected = [
        generate.CLAUDE_BIN, "-p", "PROMPT-TEXT",
        "--model", generate.SCRIPT_GEN_MODEL,
        "--max-turns", str(generate.SCRIPT_GEN_MAX_TURNS),
        "--tools", "",
        "--strict-mcp-config",
        "--system-prompt", generate.SCRIPT_GEN_SYSTEM_PROMPT,
        "--output-format", "stream-json", "--verbose",
    ]
    assert captured["argv"] == expected, (
        f"default-path argv drifted:\n  got      {captured['argv']}\n  expected {expected}")
    # The Popen kwargs matter as much as the argv: utf-8/replace is what keeps Vietnamese
    # from being silently corrupted by the Windows cp1252 default.
    assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["stdin"] == generate.subprocess.DEVNULL


def test_script_cache_key_default_matches_pre_gate_hash():
    """An unset provider must hash to the SAME cache key as before the gate.

    The expected value is recomputed here the way `_script_cache_key` did it BEFORE it
    took provider/model arguments (hardcoded LLM_PROVIDER_ID + SCRIPT_GEN_MODEL). If this
    ever fails, every cached script batch on disk was silently invalidated."""
    parts = {"edit_mode": "recap", "word_budget": 1161, "ratio_nudge": "",
             "source_transcript_window": [0.0, 600.0], "batch_index": 0}
    pre_gate = {**parts, "provider": "claude-cli",
                "model": generate.SCRIPT_GEN_MODEL,
                "prompt_version": generate.PROMPT_VERSION}
    expected = hashlib.sha256(
        json.dumps(pre_gate, sort_keys=True).encode()).hexdigest()[:16]

    assert generate._script_cache_key(parts) == expected
    # Explicitly-resolved default pair must hash identically to "no arguments".
    prov, model = llm_gate.resolve(None, None)
    assert generate._script_cache_key(parts, provider=prov, model=model) == expected


def test_llm_kwargs_empty_when_no_choice():
    """No choice -> {} -> the call shape into _run_claude_script / _run_batches_parallel
    is literally unchanged, which is why the existing stubs in this suite still match."""
    assert generate._llm_kwargs(None, None) == {}
    assert generate._llm_kwargs("", "  ") == {}
    assert generate._llm_kwargs("gemini", None) == {"provider": "gemini", "model": None}
    assert generate._llm_kwargs(None, "gemini-flash-latest") == {
        "provider": None, "model": "gemini-flash-latest"}


def test_default_request_models_carry_no_llm_choice():
    """The per-job fields default to None on every text script-gen request model, so an
    existing caller that never heard of them produces the claude-cli path."""
    for req in (generate.ScriptRequest(topic="t"),
                generate.TransformRequest(transcript="x"),
                generate.TransformFootageRequest(segments=[])):
        assert req.llmProvider is None
        assert req.llmModel is None


# =========================================================================== #
# 2. PROVIDER ROUTING                                                         #
# =========================================================================== #

def test_gemini_routes_to_http_and_never_spawns_a_process(monkeypatch):
    """provider=gemini -> one OpenAI-compatible POST, NO subprocess at all."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-gemini")

    def _boom(*a, **k):  # any Popen here is a routing bug
        raise AssertionError("gemini must not spawn a claude -p process")

    monkeypatch.setattr(generate.subprocess, "Popen", _boom)
    resp = _FakeResponse(200, _chat_envelope(json.dumps(_VALID_SCENES)))
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))

    scenes = generate._run_claude_script("PROMPT", timeout=30, provider="gemini")
    assert scenes == _VALID_SCENES

    assert len(_FakeClient.posts) == 1
    post = _FakeClient.posts[0]
    assert post["url"].endswith("/chat/completions")
    assert "generativelanguage.googleapis.com" in post["url"]
    assert post["headers"]["Authorization"] == "Bearer test-key-gemini"
    assert post["body"]["model"] == "gemini-flash-latest"
    # The system prompt is passed as a message (the CLI passes it via --system-prompt).
    assert post["body"]["messages"][0]["role"] == "system"
    assert post["body"]["messages"][-1]["content"] == "PROMPT"


def test_openrouter_sends_require_parameters_and_reasoning_exclude(monkeypatch):
    """Both flags are load-bearing, not cosmetic: structured-output support is a property
    of the ENDPOINT (require_parameters keeps us off an incapable one), and
    reasoning.exclude strips chain-of-thought so the parser only sees candidate JSON."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-or")
    resp = _FakeResponse(200, _chat_envelope(json.dumps(_VALID_SCENES),
                                             model="some/model:free"))
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))

    res = llm_gate.run_llm_json("PROMPT", timeout=30, provider="openrouter",
                                model="some/model:free")
    assert res.data == _VALID_SCENES
    assert res.provider == "openrouter" and res.model == "some/model:free"

    body = _FakeClient.posts[0]["body"]
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"exclude": True}
    assert body["max_tokens"] == (llm_gate.LLM_MAX_OUTPUT_TOKENS
                                  + llm_gate.OPENROUTER_MAX_TOKENS_BONUS)
    headers = _FakeClient.posts[0]["headers"]
    assert headers["Authorization"] == "Bearer test-key-or"


def test_missing_api_key_fails_and_never_falls_back(monkeypatch):
    """A job naming a provider whose key is absent must FAIL with a clear message — never
    silently run on claude-cli instead. This is the no-surprise rule."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_AI_STUDIO_API_KEY", raising=False)

    def _boom(*a, **k):
        raise AssertionError("must not fall back to the claude -p path")

    monkeypatch.setattr(generate.subprocess, "Popen", _boom)

    with pytest.raises(llm_gate.LLMError) as ei:
        llm_gate.run_llm_json("PROMPT", timeout=10, provider="gemini")
    assert ei.value.status_code == 400
    assert "GEMINI_API_KEY" in ei.value.detail
    assert ei.value.retryable is False


def test_openrouter_without_a_model_is_an_explicit_error(monkeypatch):
    """OpenRouter's free catalogue churns weekly, so there is no safe hardcoded default:
    a NULL model must produce a clear 'pick one' error, not a guess."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-or")
    monkeypatch.setattr(llm_gate, "OPENROUTER_DEFAULT_MODEL", None)
    with pytest.raises(llm_gate.LLMError) as ei:
        llm_gate.run_llm_json("PROMPT", timeout=10, provider="openrouter")
    assert ei.value.status_code == 400
    assert "GET /api/llm/models" in ei.value.detail


def test_cache_key_differs_per_provider_and_model():
    """The whole reason the key was extended: a job that switches provider must never be
    served the other provider's cached scenes."""
    parts = {"edit_mode": "recap", "word_budget": 1000, "batch_index": 0}
    k_default = generate._script_cache_key(parts)
    k_gemini = generate._script_cache_key(parts, provider="gemini",
                                          model="gemini-flash-latest")
    k_gemini2 = generate._script_cache_key(parts, provider="gemini",
                                           model="gemini-3.5-flash")
    k_or = generate._script_cache_key(parts, provider="openrouter", model="x/y:free")
    assert len({k_default, k_gemini, k_gemini2, k_or}) == 4


def test_run_claude_script_uses_the_resolved_provider_in_the_cache_key(monkeypatch,
                                                                       tmp_path):
    """End-to-end guard for the Phase-0 cache-key fix: routing through the gate must key
    the cache on the RESOLVED provider/model, not on the hardcoded claude-cli pair."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-gemini")
    monkeypatch.setattr(generate, "_SCRIPT_CACHE_DIR", str(tmp_path))
    resp = _FakeResponse(200, _chat_envelope(json.dumps(_VALID_SCENES)))
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))

    parts = {"edit_mode": "recap", "word_budget": 1000, "batch_index": 0}
    generate._run_claude_script("PROMPT", timeout=30, cache_parts=parts,
                                provider="gemini")

    expected_key = generate._script_cache_key(parts, provider="gemini",
                                              model="gemini-flash-latest")
    written = {p.name for p in tmp_path.iterdir()}
    assert f"{expected_key}.json" in written, (
        f"cache was written under the wrong key: {written}")
    assert f"{generate._script_cache_key(parts)}.json" not in written, (
        "gemini scenes were written under the claude-cli cache key")


def test_object_wrapped_array_is_unwrapped(monkeypatch):
    """OpenAI-style structured output needs an OBJECT root, so a model asked for an array
    may legitimately answer {"scenes": [...]}. Unwrapping it is not guessing."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    resp = _FakeResponse(200, _chat_envelope(json.dumps({"scenes": _VALID_SCENES})))
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))
    res = llm_gate.run_llm_json("P", timeout=10, provider="gemini")
    assert res.data == _VALID_SCENES


def test_secret_scan_refuses_to_send_a_credential(monkeypatch):
    """A prompt carrying anything credential-shaped must never leave the machine — free
    tiers may train on and publish prompts."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")

    def _boom(**k):
        raise AssertionError("the request must not be sent at all")

    monkeypatch.setattr(llm_gate.httpx, "Client", _boom)
    with pytest.raises(llm_gate.LLMError) as ei:
        llm_gate.run_llm_json("here is my key sk-or-v1-abcdefghijklmnopqrstuvwxyz",
                              timeout=10, provider="gemini")
    assert ei.value.status_code == 400
    assert "REFUSING" in ei.value.detail


# =========================================================================== #
# 3. ERROR TAXONOMY                                                           #
# =========================================================================== #

@pytest.mark.parametrize("status,retryable", [
    (429, True), (500, True), (502, True), (503, True), (504, True),
    (400, False), (401, False), (403, False), (404, False), (413, False),
])
def test_http_status_retryability(status, retryable):
    err = llm_gate._classify_http(status, "boom", provider="gemini", model="m")
    assert err.retryable is retryable
    assert llm_gate.is_retryable(err) is retryable


def test_generate_is_retryable_honors_the_llm_error_flag():
    """LLMError subclasses HTTPException, so it MUST be checked before the CLI's
    'status_code == 504' rule — otherwise a retryable 429 would be judged non-retryable
    and a non-retryable 504-shaped LLMError would be retried."""
    retry_429 = llm_gate.LLMError(429, "rate limited", provider="gemini", retryable=True)
    hard_400 = llm_gate.LLMError(400, "bad request", provider="gemini", retryable=False)
    assert generate._is_retryable(retry_429) is True
    assert generate._is_retryable(hard_400) is False
    # The claude-cli rule is untouched.
    assert generate._is_retryable(HTTPException(504, "timeout")) is True
    assert generate._is_retryable(HTTPException(500, "boom")) is False


def test_provider_failure_message_names_the_provider(monkeypatch):
    """A non-default provider failing out of retries must produce a message that NAMES it
    and says nothing else was tried — the owner must never wonder what actually ran."""
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(generate, "SCRIPT_GEN_RETRIES", 0)
    monkeypatch.setattr(generate.time, "sleep", lambda *a, **k: None)
    resp = _FakeResponse(429, None, text="rate limited")
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))

    with pytest.raises(HTTPException) as ei:
        generate._run_claude_script("P", timeout=10, provider="gemini")
    detail = ei.value.detail
    assert "gemini/gemini-flash-latest" in detail
    assert "Không tự động đổi sang model khác" in detail


def test_truncated_output_is_reported_as_truncation():
    """'Wrong shape' and 'ran out of output tokens' need different fixes, so they must not
    be reported the same way."""
    with pytest.raises(llm_gate.LLMError) as ei:
        llm_gate._parse_json_array('[{"scene": 1, "narration": "xin ch',
                                   provider="openrouter", model="m")
    assert "TRUNCATED" in ei.value.detail
    assert ei.value.retryable is True


def test_empty_content_is_reported_as_a_reasoning_budget_burn(monkeypatch):
    """The measured OpenRouter failure mode: content=null, finish_reason='length'."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    resp = _FakeResponse(200, _chat_envelope(None, model="x:free", finish="length"))
    monkeypatch.setattr(llm_gate.httpx, "Client", lambda **k: _FakeClient(resp, **k))
    with pytest.raises(llm_gate.LLMError) as ei:
        llm_gate.run_llm_json("P", timeout=10, provider="openrouter", model="x:free")
    assert "chain-of-thought" in ei.value.detail
    assert ei.value.retryable is True


# =========================================================================== #
# 4. CATALOGUE (GET /api/llm/models)                                          #
# =========================================================================== #

def test_catalogue_always_has_exactly_one_default_and_it_is_claude_cli(monkeypatch,
                                                                      tmp_path):
    monkeypatch.setattr(llm_gate, "_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_AI_STUDIO_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    payload = llm_gate.list_model_options(force_refresh=True)
    defaults = [o for o in payload["options"] if o["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["provider"] == "claude-cli"
    assert defaults[0]["model"] is None
    # No key -> the provider is ABSENT, never listed-but-broken.
    assert {o["provider"] for o in payload["options"]} == {"claude-cli"}
    assert payload["generated_at"]


def test_gemini_options_are_the_allowlist_intersected_with_the_live_list(monkeypatch,
                                                                         tmp_path):
    """The fixed allowlist defends against 'advertised but not callable'; intersecting
    with ListModels additionally hides a fully retired model. Both directions matter."""
    monkeypatch.setattr(llm_gate, "_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Live list drops one allowlist entry and advertises an extra model we must NOT add.
    monkeypatch.setattr(llm_gate, "_live_gemini_models",
                        lambda key, timeout=20: {"gemini-flash-latest",
                                                 "gemini-3.6-flash",
                                                 "gemini-3.5-flash",
                                                 "gemini-2.5-flash"})
    payload = llm_gate.list_model_options(force_refresh=True)
    gem = [o["model"] for o in payload["options"] if o["provider"] == "gemini"]
    assert "gemini-3.1-flash-lite" not in gem, "a retired model was not hidden"
    assert "gemini-2.5-flash" not in gem, "a non-allowlisted live model leaked in"
    assert gem == ["gemini-flash-latest", "gemini-3.6-flash", "gemini-3.5-flash"]
    assert all(o["reliability"] == "high" for o in payload["options"]
               if o["provider"] == "gemini")


def test_openrouter_options_are_always_low_reliability(monkeypatch, tmp_path):
    """Unconditional, whatever discovery reports — Phase 1 measured the free models
    failing this task shape."""
    monkeypatch.setattr(llm_gate, "_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(llm_gate, "_live_openrouter_free",
                        lambda **kw: [{"id": "vendor/big:free", "ctx": 262144,
                                       "structured": True, "uptime_1d": 99.9}])
    payload = llm_gate.list_model_options(force_refresh=True)
    ors = [o for o in payload["options"] if o["provider"] == "openrouter"]
    assert ors and all(o["reliability"] == "low" for o in ors)
    assert all(o["is_default"] is False for o in ors)


def test_catalogue_is_disk_cached(monkeypatch, tmp_path):
    """A dashboard page load must not cost a round of provider calls."""
    monkeypatch.setattr(llm_gate, "_LLM_CACHE_DIR", str(tmp_path))
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    calls = []
    monkeypatch.setattr(llm_gate, "_live_gemini_models",
                        lambda key, timeout=20: calls.append(1) or set(llm_gate.GEMINI_MODELS))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    first = llm_gate.list_model_options(force_refresh=True)
    second = llm_gate.list_model_options()
    assert first["cached"] is False and second["cached"] is True
    assert len(calls) == 1, "the cached read hit the provider anyway"
    assert second["options"] == first["options"]
