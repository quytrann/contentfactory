# Video Production — Lessons Learned

A living log of problems found while producing real videos, and what we changed to
prevent them. Each entry: **Symptom → Root cause → Fix applied → Future improvement.**

> **IMPORTANT — fixes are not retroactive.** These fixes live in code paths (the
> `generate.py` script-generation prompts and the media-engineer's TTS/assembly path).
> They only take effect when a video is produced *through the patched pipeline*. The
> existing 14-minute video (9-min target) was made before these fixes — it must be
> **regenerated** through the updated pipeline to benefit. Re-running ingest → script →
> TTS → assemble is required; editing the old output file does nothing.

---

## 1. Original source audio bleeding through (two voices)

- **Symptom:** The finished video plays the Vietnamese narration *and* the original
  source audio underneath at the same time — two voices talking over each other.
- **Root cause:** The assembly step mixed the source clip's audio at a hardcoded
  `0.15` (15%) level for every video, with no way to turn it off.
- **Fix applied (media side):** Source audio is now **muted by default**, with an
  opt-in `srcAudioVolume` option (0 / 5 / 10 / 15 %). Default 0 = clean narration only.
- **Future improvement:** Per-video judgement on when faint source audio actually helps
  (e.g. ambient game/match sound under commentary) vs. when it's just noise — surface
  the choice in the Studio form with a sensible per-mode default.

## 2. Garbled / looped audio + trailing silence (e.g. 1:28–1:42)

- **Symptom:** A stretch of narration rambles, repeats a phrase in a loop, then pads
  with silence.
- **Root cause:** VieNeu-TTS hits its `max_new_frames` cap (~24 s of audio per call)
  when a single per-scene narration string is too long. Past the cap it degrades —
  rambling, looping the last phrase, and emitting silence to fill.
- **Fix applied (media side):** Synthesize **sentence-by-sentence** instead of one long
  per-scene call, and **trim trailing silence** from each piece before concatenating.
- **Future improvement:** Keep per-scene narration short at the script stage (the new
  word budget helps); watch the per-call duration cap; evaluate **F5-TTS** for long lines
  since it handles longer inputs more gracefully.

## 3. Long gap inside a word (e.g. "ít……ỏi" at 1:56–2:00)

- **Symptom:** A caption highlight stretches across a multi-second pause in the middle
  of a single word — the word visibly "tears" with a gap.
- **Root cause:** The 24 s-cap silence (issue #2) created dead air, and the karaoke
  caption highlight set each word's `end` to the *next* word's `start` — so a highlight
  spanned the whole silence gap.
- **Fix applied (media side):** Clamp the karaoke highlight `end` so it can't stretch
  across a silence, plus per-sentence TTS (which removes most of the gaps to begin with).
- **Future improvement:** Caption timing should *never* stretch a highlight across a
  silence — make that an invariant in the caption builder regardless of TTS behavior.

## 4. Freeze frame (e.g. 2:14–2:33)

- **Symptom:** The video freezes on a single source frame for ~20 s while narration
  continues.
- **Root cause:** When the source clip for a scene was **shorter than the voiceover**,
  assembly held the last frame (`tpad=clone`) to fill the remaining VO duration.
- **Fix applied (media side):** **Loop the source footage** to cover the full VO length
  instead of freezing on the last frame.
- **Future improvement:** Ensure footage length ≥ VO length per scene at planning time;
  shorter per-sentence VO (issue #2) also reduces how often footage falls short.

## 5. Output far longer than target (9 min target → 14:23 actual)

- **Symptom:** A video requested at ~9 minutes rendered at 14 minutes 23 seconds.
- **Root cause:** Mostly the 24 s-capped **dead-air scenes** (issue #2) inflating
  runtime; the residual was an **over-long script** — too much narration written for the
  requested duration.
- **Fix applied:**
  - *Media side:* no more silence padding (issue #2), so dead air no longer inflates.
  - *Script side (this batch):* FIXED mode now enforces a hard **word budget** derived
    from the target duration — Vietnamese narration rate **~2.5 words/sec (≈150 wpm)**,
    so `max_total_words ≈ durationSec × 2.5`. The prompt instructs the model to **cut
    content, not pad**, and stay within budget; scene count is capped so it never
    requests more scenes than the budget can fill (≥ 8 words/scene). AUTO mode keeps the
    "follow source / trim redundancy" behavior (no fixed budget).
- **Future improvement:** After assembly, **validate the final rendered duration against
  the target**; if it's over by more than a tolerance, auto-trim or regenerate the script
  with a tighter budget.

## 6. English technical terms mistranslated / misspelled in captions

- **Symptom:** Terms like "agent harness", "context window", "token", "prompt", product
  and library names came out translated or Vietnamized — TTS then mispronounced them and
  captions spelled them wrong.
- **Root cause:** The script prompt didn't tell the model to preserve technical terms, so
  it translated/transliterated them like ordinary words.
- **Fix applied (prompt-side mitigation, this batch):** Both the transform and footage
  script prompts now instruct: **keep technical terms / proper nouns / product names in
  their original English**, embedded naturally inside Vietnamese sentences — do not
  translate, transliterate, or Vietnamize. This keeps TTS pronunciation and caption
  spelling correct.
- **Future improvement:** A **per-page term glossary** (canonical spelling/casing) fed
  into the prompt; automatic term detection from the source transcript so the glossary
  populates itself.

## 7. Promotional / subscribe / dead segments included

- **Symptom:** Runtime wasted on "like and subscribe to the original channel", sponsor
  reads, channel self-promo, intros/outros, and dead air carrying no content.
- **Root cause:** The script prompt didn't tell the model to skip non-content segments,
  so they were narrated and (for footage mode) selected as source windows.
- **Fix applied (prompt-side mitigation, this batch):** Both prompts now instruct the
  model to **omit promo / subscribe / sponsor / self-promo / intro-outro / dead-air
  segments entirely** — no narration and no scene for them. The footage prompt
  additionally forbids selecting those time windows as `sourceStart`/`sourceEnd` footage.
- **Future improvement:** **Auto-detect** promo/sponsor segments from the transcript
  (keyword + pattern heuristics, or a classifier pass) and strip their source windows
  before the script stage even sees them.

## 8. F5-TTS voice clone "echoes" the reference instead of speaking the script

- **Symptom:** A cloned voice (e.g. "Tourist - F5-TTS") returned HTTP 200 but the synth
  output ignored the requested text and instead spoke the *reference clip's* words.
- **Root cause:** F5-TTS sets output length from `ref_len * (ref_text_len + gen_text_len)
  / ref_text_len`, so a **long, low-text-density reference** blows up the frame budget and
  F5 regenerates the reference. The "Tourist" ref was ~11 s carrying only ~70 chars
  (~6 ch/s) — a slow subscribe-outro line — vs working refs at ~22–27 ch/s. F5's own
  clipper only triggers above 12 s, so the 11 s ref slipped through uncapped.
- **Fix applied (this batch):** `tts_worker._prep_f5_ref()` silence-trims and **hard-caps
  the F5 reference to `F5_REF_MAX_SEC` (default 6 s)** before inference, and transcribes
  the *capped* clip for ref_text so text and audio agree. Falls back to the original ref
  on any ffmpeg error. Verified: Tourist now speaks the script (3/3); known-good voices
  unaffected.
- **Future improvement:** Prefer **dense** reference clips (more words spoken in 6–8 s, no
  slow pacing/outro). Optionally score a clone's ref density at upload time and warn if too
  low. A low-density ref may still leave a tiny garbled head artifact (cosmetic).

---

### Prompt change reference (this batch)

Edited `Dashboard/api/generate.py` script-generation prompts only (not assembly/TTS):

- New constants: `_VI_WORDS_PER_SEC = 2.5`, `_MIN_WORDS_PER_SCENE = 8`,
  `_KEEP_ENGLISH_TERMS`, `_CUT_PROMO` (+ `_CUT_PROMO_FOOTAGE` variant), and a
  `_word_budget(durationSec)` helper.
- `_build_transform_prompt` / `_build_footage_prompt`: FIXED mode now appends the word
  budget; both modes (AUTO + FIXED) append keep-English-terms and cut-promo to the safety
  rules. AUTO mode and `summary` keep their existing "follow source / trim redundancy"
  behavior — no fixed budget.
- `_auto_scene_count`: in FIXED mode, scene count is now also capped by the word budget so
  per-scene narration stays substantive.

## 9. Re-cloning a voice (same name) reused a STALE ref_text → wrong/garbled output

- **Symptom:** After re-cloning "Tourist" (uploading a new reference under the same name),
  the F5 voice still output wrong/echoed audio.
- **Root cause:** the F5 ref_text sidecar `_reftext/<name>.txt` was keyed by FILENAME only.
  Re-cloning the same name reused the same path, so the OLD clip's transcript was served and
  the new (capped) clip was never re-transcribed → text↔audio mismatch (exactly what makes
  F5 echo the reference). Intermittent: only triggers when the new clip differs from the
  stale text.
- **Fix applied:** sidecar now stores a content **fingerprint** (`# fp:<size>:<mtime_ns>`)
  and is only reused when it still matches the current clip; a changed clip auto-busts it.
  Plus `upload_voice` now deletes the stale `_reftext` sidecar AND `_previews/clone_*.wav`
  on every (re-)upload. Verified: re-cloning with a different ref now speaks the script.
- **Future improvement:** key all per-voice caches by content hash, not name, project-wide.

## 10. F5-TTS intermittent cuDNN load failure (Error code 127)

- **Symptom:** F5 synth occasionally fails with `Could not load symbol cudnnGetLibConfig.
  Error code 127`, succeeds on immediate retry — can look like "the voice is broken".
- **Root cause:** transient cuDNN/CUDA library-load flake at worker process start (clears in
  a fresh process). Aggravated if the API is launched from a shell without a normal user PATH.
- **Fix applied:** `_run_cf_worker` now retries transient GPU-load errors up to 2x (3 attempts,
  fresh subprocess each, 2s backoff), ONLY for load-error signatures — genuine input errors
  (missing ref, empty ref_text, model-not-found) still fail fast. Exhaustion → clean HTTP 503
  with an explanatory message. `_warm_clone_preview` now logs failures instead of swallowing.
- **Future improvement:** keep a warm long-lived F5 process for batch jobs; always launch the
  API from a normal user shell (full PATH for bundled cuDNN).

## 11. Narration reads too fast — viewers can't keep up (HARD RULE)

- **Hard principle (never violate):** *Making a video shorter must NEVER be achieved by
  compressing content into faster speech.* To shorten a video, **reduce/curate content**
  (drop less-important points, cut filler) or **let the video run longer** — never raise
  the reading/voice speed to cram more content into less time. Fast narration loses the
  viewer; a short runtime is a content decision, not a playback-speed decision.
- **Symptom:** Narration is intelligible but rushed — viewers can't keep up, especially on
  technical terms and numbers. The video "fits" the target duration only because every
  sentence is spoken too fast and the script is over-dense.
- **Wrong approach (what caused this):** Hit a short target by (a) **speeding up the voice**
  and/or (b) **cramming more words** into the target seconds. Both compress content into
  faster speech instead of cutting it.
- **Right approach:** Keep a **comfortable Vietnamese pace (~2.0–2.2 words/sec)** and a TTS
  speed at or near natural (no global speed-up). If content doesn't fit, **cut/curate** it
  or **allow a longer runtime** — see the transformation rules in
  [how to edit video.md](how%20to%20edit%20video.md) (recap/summary = *select and condense
  what is said*, not *say everything faster*).
- **Root cause (two knobs that were over-tuned):**
  1. **TTS speed cap** — F5 adaptive speed-up in `Dashboard/api/workers/tts_worker.py`
     (`F5_SPEED_TARGET=17` ch/s, `F5_SPEED_MAX=1.6`) was meant to remove reference
     vowel-drag but globally sped whole sentences up to ~1.6×, compressing real content.
  2. **Word-budget pace** — `_VI_WORDS_PER_SEC=2.5` (~150 wpm) in
     `Dashboard/api/generate.py` packed words too densely, so the script crammed words to
     hit the target seconds (see issue #5, which set this budget).
- **Fix applied (shipped, verified by real synthesis + ffprobe + whisper):**
  - *Media side:* `F5_SPEED_TARGET` 17 → **14** ch/s and `F5_SPEED_MAX` 1.6 → **1.25** in
    `tts_worker.py`. Measured on the worst-case low-density ref (Tourist): speaking rate
    dropped from **1.48× / 3.82 words/sec → 1.22× / 3.14 words/sec**, with no vowel-drag
    regression. The adaptive speed-up now trims only reference drag, not full sentences.
  - *Script side:* `_VI_WORDS_PER_SEC` 2.5 → **2.1** in `generate.py` (one constant feeds
    both prompt builders and the scene-count math). The prompt must **cut content, not pad**.
- **Important nuance (learned during the fix):** F5's *intrinsic* cadence is ~3.0 words/sec
  even at 1.0× speed, so the **script-density knob does NOT slow the reading** — it only
  shortens the script and adds static hold time. Only the **TTS speed cap** changes the
  actual speaking rate. The ~2.0–2.2 "comfortable" figure is therefore a *script-length*
  target, not an achievable spoken rate with F5 alone; going below ~3.0 wps needs a
  pitch-preserved `atempo<1.0` slowdown in assembly or a denser reference clip.
- **Owner decision (2026-06-23):** **Accept ~3.14 words/sec** — the artificial over-speed
  bug is gone and this is F5's natural floor (no atempo/re-record). For fixed-duration
  videos, **keep the shorter script** and fill the slack with Ken Burns hold (videos may
  run a bit under target) rather than cram words back in. Shrinking duration must come from
  the script (fewer words) or longer runtime, **never** from raising voice speed.
- **Future improvement:** After assembly, **probe the actual spoken rate** (whisper
  word-count ÷ audio seconds) and warn if it exceeds the accepted cap — catch a too-fast
  render objectively rather than by ear.
