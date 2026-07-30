# Research: F5-TTS Pace Control & Smooth Long-Form Narration Rearchitecture

Date: 2026-07-05. Researcher agent. For: ContentFactory TTS rearchitecture (Vietnamese ViVoice/cloned checkpoint, local, RTX 2070 8GB).

## Q1 — Speed/pace mechanics (HIGH confidence, primary source)

**Duration formula (mainline F5-TTS `utils_infer.py`):**
```
ref_audio_len = audio.shape[-1] // hop_length   # hop_length = 256, sr = 24000 -> ~93.75 frames/s
duration = ref_audio_len + int(ref_audio_len / ref_text_len * gen_text_len / local_speed)
```
- `ref_text_len` / `gen_text_len` = **UTF-8 byte length** of ref/gen text (NOT chars; Vietnamese diacritics = multi-byte).
- The generated portion of the target mel length = `ref_audio_len / ref_text_len * gen_text_len / local_speed`.
- `ref_audio_len / ref_text_len` = **seconds-of-audio per byte of reference text = the reference clip's own speaking rate.** This is the root cause: pace is inherited from the ref clip. Paper confirms: "we simply estimate the duration based on the ratio of the number of characters in y_gen and y_ref." (arxiv 2410.06885)
- `local_speed` **divides** the generated length. So target length ∝ 1/speed → **speed is a linear time-scale on the allotted duration.** speed=2.0 halves allotted time (faster), speed=0.5 doubles it (slower). It sets a *duration budget*; the flow-matching model fills it. NOT a post-hoc atempo — the model re-generates prosody to fit, so it changes rhythm naturally rather than pitch-shifting.
- Short-text guard: `if len(gen_text.encode("utf-8")) < 10: local_speed = 0.3`.
- `fix_duration` param overrides the whole formula: `duration = int(fix_duration * sr / hop_length)` — lets us set an EXACT target seconds per chunk.

**Is `speed` reliable/linear?** The *duration allotment* is exactly linear in 1/speed. But whether the model *fills* the budget faithfully is not perfectly linear: at extreme budgets the model slurs (too little time) or inserts hesitation/drags vowels (too much time). No maintainer-documented "safe range", but community + paper degradation notes → keep speed within roughly **0.8–1.3** of natural; beyond that intelligibility/quality drops (issue #811 shows severe compression when the *effective* rate runs away on long text; the Cross-Lingual F5 paper notes overly-fast rates compress temporal patterns and hurt intelligibility).

**Ref-independent normalization (the key fix — DERIVED FROM THE FORMULA, HIGH confidence in the math):**
Derivation: gen mel frames = `(ref_audio_len/ref_text_len) * gen_bytes / speed`. Output rate (bytes/sec)
= `gen_bytes / output_seconds = speed * (ref_bytes/ref_seconds) = speed * R_ref`.
Therefore **output_rate = speed × R_ref**, where `R_ref = ref_text_bytes / ref_audio_seconds`.
To hit a fixed `R_target` regardless of the reference:
```
speed = R_target / R_ref
```
This makes output pace independent of how fast/slow the ref talks — the documented formula supports it directly (this is essentially rate-matching the built-in duration estimator).
Pitfall: very fast or very slow refs push required `speed` outside the safe ~0.8–1.3 band → then also normalize/atempo the REFERENCE clip first so speed stays near 1.0.

**Re-timing the reference clip (atempo the ref before cloning):** No official doc, thin community evidence. The model conditions on ref mel; atempo-stretching the ref audio (without pitch change) changes its bytes/s ratio and *does* shift inferred pace — but it can introduce ref artifacts the clone imitates. Treat as empirical fallback, prefer speed/fix_duration.

## Q2 — Smooth continuous synthesis (MIXED confidence)

- **Single long inference drifts/speeds up** (issue #811): 42→374 chars Chinese went 0.051→0.006 s/char. Root cause = the model was trained on <=30s clips; beyond that it compresses. So DON'T do one giant inference. (Training-length limit is inferred from behavior + community, not stated in paper section fetched.)
- **Mainline chunking:** `chunk_text(max_chars=135 default? actual infer_process uses a dynamic max_chars based on ref length; utils uses 135)`, splits on sentence punctuation, generates per-chunk, concatenates with **linear** cross-fade `cross_fade_duration=0.15s` default. Linear fade = equal-GAIN, dips perceived loudness at the seam (−3 dB), can sound like a beat.
- **Middle ground community rec:** chunk at a few sentences (not per-sentence, not whole doc), **fix the seed per run** to reduce timbre drift (issue #811 workaround; note #1155 says seed helps timbre little for DiT — conflicting, so treat as minor). Bigger chunks = fewer seams but more intra-chunk drift; sweet spot empirically ~2–4 sentences / under ~200 gen bytes.
- **Click-free concat best practice (audio-engineering, HIGH confidence):** remove DC offset first; align cut at zero-crossing; use **equal-power (sinus/√) crossfade**, not linear; 2–4 ms (~100–200 samples @24k ≈ 48–96 samples) minimal for de-click, but for TTS chunk joins a longer 15–30 ms equal-power fade hides prosodic seams better. F5's own 0.15s linear is long but equal-gain; replacing with equal-power same length is an easy win.
- **Reducing stochastic within-sentence hesitations (LOW-MED confidence):** No generation-side "no hesitation" switch. Levers: higher `cfg_strength` (>2.0) → more stable, less expressive (paper); `nfe_step` 32 default, going higher (48–64) can smooth prosody marginally at cost of speed; `sway_sampling_coef=-1.0` is optimal (paper ablation, don't change). Best practical lever = **generate 2–3 seeds per chunk and pick the one whose whisper-measured word rate is most uniform** (empirical). Hesitations are partly speech (vowel drag) that shaping can't fix — matches project MEMORY note f5-mid-sentence-gaps.

## Q3 — Karaoke alignment (HIGH confidence)

Use case = we KNOW the text, want word timestamps for CAPTION timing only (not cutting audio).
- **ctc-forced-aligner (MahmoudAshraf97):** true forced alignment to known text, model `MahmoudAshraf/mms-300m-1130-forced-aligner` (MMS/wav2vec2 CTC). Vietnamese supported. 5x less memory than torchaudio FA. Needs romanization flag for multilingual model even though Vietnamese is Latin-with-diacritics. Deterministic, robust because text is given — best fit for pure timing.
- **CrisperWhisper (nyrahealth):** verbatim ASR w/ DTW-on-cross-attention timestamps; AMI seg F1 0.79 @50ms collar (state-of-art word boundaries, near-perfect filled-pause detection). But it TRANSCRIBES (may disagree with known text) → for known-text timing, forced alignment is the cleaner contract.
- **faster-whisper:** what project already uses; word timestamps are heuristic (less precise at boundaries) but zero new deps.
- **Recommendation:** for karaoke precision on known Vietnamese text → **ctc-forced-aligner (MMS vie)**. Fallback = faster-whisper (already installed). CrisperWhisper only if we want verbatim re-transcription, not our case.

## Sources
- Paper: https://arxiv.org/pdf/2410.06885 (duration = ratio; sway s=-1 best; 16 NFE viable)
- utils_infer.py: https://github.com/SWivid/F5-TTS/blob/main/src/f5_tts/infer/utils_infer.py
- infer README: https://github.com/SWivid/F5-TTS/blob/main/src/f5_tts/infer/README.md (ref <12s + 1s trailing silence)
- #811 long-text speedup: https://github.com/SWivid/F5-TTS/issues/811
- #1155 short-text speed / seed: https://github.com/SWivid/F5-TTS/issues/1155
- #876 consistent speed: https://github.com/SWivid/F5-TTS/issues/876
- #402 socket high rate: https://github.com/SWivid/F5-TTS/issues/402
- Cross-Lingual F5 (speaking-rate predictor VARIANT, not mainline): https://arxiv.org/html/2509.14579
- ctc-forced-aligner: https://github.com/MahmoudAshraf97/ctc-forced-aligner
- CrisperWhisper: https://arxiv.org/abs/2408.16589
- Crossfade/zero-cross/DC: Audacity manual, SoundOnSound
