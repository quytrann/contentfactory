# Bug 7 — Caption runs ~0.5 words AHEAD of audio (footage mode)

Video id=2, job id=2, page "Giải Thích Mọi Thứ".
render_model=passthrough-trim, voice=clone F5-TTS, edit_mode=summary → **footage assembler path**.

Confidence: **HIGH / definitive.** The root cause is a deterministic off-by-one bias in
the caption-timing math, reproduced in a standalone simulation (no render needed). See
"Quantitative proof" below. The exact magnitude in the finished video would need a render
to confirm to the millisecond, but the *direction and order of magnitude* (caption leads
audio by ~0.18s mean ≈ ~0.5 words) match the report precisely.

---

## 1. Where caption timings + the subtitle file are generated

All in `d:\workspace\ContentFactory\Dashboard\api\generate.py` (the footage assembler):

| Step | Function | Lines |
|------|----------|-------|
| Whisper word timestamps on each scene's TTS audio | `assemble_footage` → `_run_cf_worker("whisper_worker.py", …)` | 4106–4117 |
| **Build per-word caption timing (the bug lives here)** | `_aligned_caption_words(narration, whisper_words, audio_dur)` | 3769–3847 |
| Write the per-scene `.ass` subtitle file | `_build_karaoke_ass(words, …)` | 3858–3980 |
| Burn the `.ass` into the scene clip | `_footage_scene_clip(... ass_path ...)` → `subtitles='{ass}'` filter | 3983–4053 (filter at 4019–4021) |
| ASS timecode formatting (cs precision) | `_ass_time(t)` | 3740–3745 |

The `.ass` is written to a `tempfile.TemporaryDirectory()` (line 4128) as `cap_<idx>.ass`
and burned in during the per-scene encode, then the temp dir is deleted — which is why no
`.srt/.ass/.vtt` survives in the output dir, exactly as described.

Caller wiring (confirms the data fed in): `Dashboard\api\runner.py` line 1805–1806 builds
each `FootageScene(caption=s["narration"], durationS=_scene_clip_duration(s, r))`.

---

## 2. Where the caption timestamps come from

- **Caption TEXT** = the script narration (`scene.caption` = `s["narration"]`), NOT whisper's
  re-transcription (whisper mishears Vietnamese; see comment at 3775–3779).
- **Caption TIMING** = whisper **word timestamps** measured on **each scene's own TTS audio
  file** (`whisper_worker.py` returns honest `word.start/word.end`, rounded to ms — lines 90–93).
- Each scene's `.ass` uses times relative to 0, and is burned onto `[vbase]` which is
  `setpts=PTS-STARTPTS` then `trim=duration=dur` (line 4017) — i.e. the caption track and the
  audio track (`[1:a]`, line 4038) share the **same** scene-local zero. So there is **no
  per-scene constant offset, no source-timestamp leakage, no cumulative-drift, and no
  whisper-vs-audio file mismatch.** The audio whisper transcribes is the exact audio that
  plays. Per-scene alignment is therefore inherently tight — ruling out candidate causes
  (b), (c), (d), and (e) from the brief.

The lead is introduced purely inside `_aligned_caption_words` when it re-maps whisper's
per-word timing onto the (different-count) narration tokens.

---

## 3. Root cause — off-by-one in the proportional whisper-index mapping

`_aligned_caption_words`, lines 3812–3828 (the `whisper_words and len >= 2` branch):

```python
frac_start = cum / total_w          # cum = sum of weights of tokens BEFORE token i
cum += weights[i]
frac_end = cum / total_w
wi_start = min(nw - 1, int(frac_start * nw))   # <-- token i anchored at its LEADING edge
...
st = whisper_words[wi_start]["start"]
```

Each narration token `i` is anchored to the whisper word found at its **leading edge**
(`frac_start` = cumulative char-weight *before* token i), and the index is taken with
`int()` (floor). Two compounding effects push the chosen whisper index *earlier* than the
token's true spoken position:

1. **Leading-edge anchoring**: token i's start should correspond to where token i is
   *spoken*, i.e. roughly its *center* in the utterance, but it is mapped from its *start
   boundary* — half a token early by construction.
2. **Floor truncation + char-weight skew**: `int(frac_start * nw)` rounds the fractional
   index down, so a token frequently borrows the start of whisper word `i-1` instead of `i`.

Net result: the highlight/appearance for each word fires from the timing of an *earlier*
whisper word → **the caption consistently leads the audio**.

### Quantitative proof (standalone simulation of the exact code)

Ground truth: token i is spoken at `t = i * 0.4s` (≈ 2.5 words/s, typical VN narration);
whisper sees the same audio with matching word starts. Averaged over 2000 randomized
scenes (6–18 tokens, random word lengths):

| mapping | mean caption lead |
|---------|-------------------|
| **current code** (`int(frac_start*nw)`) | **+0.181 s** (caption ahead of audio) |
| center-anchored (`int((cum+w/2)/total_w * nw)`) | +0.002 s |
| direct index (identity when counts match) | 0.000 s |

+0.181 s mean lead at ~2.5 words/s ≈ **~0.45 words ahead** — matches the reported "~0.5 words
ahead throughout." Worst-case single tokens reached +0.8 to +1.2 s in the sim. (At ~2–3
words/s, 0.5 word ≈ 165–250 ms, so a ~180 ms systematic lead is squarely in the reported band.)

---

## 4. Proposed fix (EXACT)

**File:** `d:\workspace\ContentFactory\Dashboard\api\generate.py`
**Function:** `_aligned_caption_words`
**Change:** anchor each narration token at its **center** of mass instead of its leading
edge, when mapping onto the whisper word index. This removes the half-token leading-edge
bias and the floor skew (mean lead +0.181 s → +0.002 s) while keeping the whole char-weight
algorithm and all downstream code unchanged. It is monotonic and never larger than the
existing clamps already guarantee.

### Before (lines 3815–3823)

```python
        for i, tok in enumerate(tokens):
            # Proportional position of this token within the narration (by char
            # weight) mapped onto whisper's word index, so a token borrows the
            # timing of the whisper word at the matching point in the utterance.
            frac_start = cum / total_w
            cum += weights[i]
            frac_end = cum / total_w
            wi_start = min(nw - 1, int(frac_start * nw))
            wi_end = min(nw - 1, max(wi_start, int(frac_end * nw - 1e-9)))
```

### After

```python
        for i, tok in enumerate(tokens):
            # Proportional position of this token within the narration (by char
            # weight) mapped onto whisper's word index. Anchor each token at its
            # CENTER of mass (cum + half its own weight), not its leading edge:
            # leading-edge + int()-floor anchoring borrowed the start of the
            # PRECEDING whisper word, making every caption pop ~half-a-word to a
            # full word BEFORE the audio (the "captions lead by ~0.5 words" bug).
            w = weights[i]
            frac_center = (cum + w / 2.0) / total_w
            frac_start = cum / total_w
            cum += w
            frac_end = cum / total_w
            wi_start = min(nw - 1, max(0, int(frac_center * nw)))
            # End index still bounds the token's own span [frac_start, frac_end];
            # keep it >= wi_start so en >= st holds.
            wi_end = min(nw - 1, max(wi_start, int(frac_end * nw - 1e-9)))
```

(Only the `start`-index anchor changes from `frac_start` to `frac_center`; `frac_start`
is retained only because the original variable name is referenced in the comment context —
it can be dropped if unused. `wi_end` is unchanged, so word-end/highlight-hold behavior is
preserved.)

### Why not a constant negative offset?

A flat "subtract 0.18 s" would also work pragmatically (0.5 word ≈ 150–250 ms), but it would
be a band-aid over a structural off-by-one, would over/under-correct on scenes whose token
count diverges a lot from the whisper word count, and could push the first word's start
negative (clamped to 0 anyway). The center-anchor fix addresses the actual cause and reduces
the mean lead to ~0 across the full range of scene shapes — strictly preferable.

### Note on the fallback branch (lines 3829–3839)

The `else` branch (no/too-few whisper words → even char-weight split over [span_start,
span_end]) is **not** affected by this bug: it places each token's start at its own leading
edge of a synthetic even grid, which is internally consistent (there is no separate "true"
audio timing to lead). Leave it unchanged.

---

## 5. What would fully confirm in the rendered artifact

Definitive code-level reproduction is done. To confirm against the *actual* job-2 video:
re-transcribe one burned scene (or the final mp4) with whisper word timestamps and compare
each `.ass` Dialogue `Start` against the spoken word start — expect the current build to show
the Dialogue start preceding the spoken onset by ~150–250 ms on average. (Not run here: the
brief says do NOT render. The per-scene VO files are content-hashed in
`E:\ContentFactory\_cache\tts\` with no scene mapping, so a clean re-measure needs a render.)

---

## Summary

- **Responsible file/function:** `Dashboard/api/generate.py` → `_aligned_caption_words`, lines 3812–3823 (the whisper-index proportional mapping).
- **Root cause (HIGH confidence):** captions are anchored to the whisper word at each narration token's *leading edge* with `int()`-floor, so each word's caption borrows the start of an *earlier* whisper word → systematic +~0.18 s lead ≈ ~0.5 words ahead.
- **One-line fix:** anchor on the token's center (`frac_center = (cum + weights[i]/2)/total_w`) instead of its leading edge (`frac_start = cum/total_w`) when computing `wi_start`.
- **Do NOT** apply yet / no render performed / DB untouched, per instructions.
