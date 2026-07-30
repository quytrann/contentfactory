# Job 122 (video 118) — 6-item diagnosis (DIAGNOSIS ONLY, no code changes)

Output: `E:\ContentFactory\Giải Thích Mọi Thứ\video\Agent Harness explained in 8min (v118).mp4` (287.4s).
Render dir with FINAL scene wavs: `E:\ContentFactory\_cache\renders\118`.
Scene map (final-video ~time): item1 "cụ thể"=scene001(~2-3s); item2 karaoke ~9-12s=scene002/003,
~37-40s=scene006; item3 engineering/tồn tại ~13s=scene003; item4 "năm 2026" ~18-19s=scene003;
item5 bờ~rôm=scene002; item6 "tùy chỉnh" ~52-53s=scene009.

## ROOT-CAUSE HYPOTHESIS — CONFIRMED (with a sharper mechanism)

The recurring mid-phrase beats AND karaoke drift share a root, but it is TWO-layered:

1. The non-punct gap shaper (`_shape_nonpunct_gaps_by_energy`) is ENERGY-only: it can only delete
   audio below -40 dB. But F5's mid-phrase "beat" between two bound words is NOT pure silence — it is
   a VOICED vowel-tail glide + breath-level tone at -9..-40 dB. MEASURED:
   - scene009 "tùy→chỉnh" CTC word-gap = 201 ms: energy is -9..-14 dB for the first ~120 ms
     (voiced glide tail of "tùy"), only the last ~80 ms drops below -40 dB. The shaper saw the 50 ms
     silent bit, floored it, and LEFT the ~120 ms voiced glide → still an audible beat.
   - scene003 "năm→2026" CTC gap = 280 ms: ~50 ms voiced tail then ~200 ms hovering -40..-52 dB
     (breath/room). The -40 dB threshold FRAGMENTS it into 55+60 ms sub-runs → under-compressed.
   => Only a WORD-BOUNDARY-aware method can compress the inter-word region (voiced tail + breath).

2. Whisper (what BOTH the shaper's DP alignment AND the karaoke word_map use) is UNRELIABLE on this
   English-heavy content, so even the word-boundary route is broken:
   - scene002 "prompt"→whisper "bờ rôm"; scene006 "prompt engineering...tool calling"→"Perum
     Engineering...tuần con linh"; scene001 "agent"→"Ây Dừng"/"Ây trìn".
   - Whisper word COUNT != narration token count on loanword scenes → karaoke uses the interpolation/
     proportional fallback → drift.

CTC forced alignment (known text + `_normalize_years`) aligns these correctly — it does NOT depend on
transcription accuracy. CTC junction gaps found where whisper/energy could not:
  scene009 tùy→chỉnh 201ms; scene003 năm→year 280ms; scene002 prompt→eng 302ms; scene006 prompt→eng 120ms.

## Per-item

1. "cụ thể." pause = SENTENCE-boundary period beat (scene001 ends clip at the period). Gap "thể."→"Người"
   = 265 ms silent (energy) / 360 ms word-to-word (CTC). Composition: CF_PS_GAP_PARA_S=0.22 inter-clip
   + F5 clip-edge tails. CF_PS_PUNCT_MAX_S=0.34 cap did NOT fire (265ms < 340ms). Owner finds even this
   too complete. FIX = lower CF_PS_GAP_PARA_S and/or tighten the punct beat target (a knob, not alignment).
   NOT an alignment problem — it's the intended period beat being too long for the owner's taste.

2. Karaoke off-beat + "rush":
   (a) KARAOKE_GAP_RUSH=0 WAS in effect — api.log shows a [startup] restart at 14:20:36, after the
       generate.py edit (13:47) and before job122 assembly (15:10). Pull-forward code is fully gated by
       `if _rush:` (verified) so it did NOT run. The "rush" is NOT the old pull-forward.
   (b) The residual off-beat/rush IS caption-timing drift from whisper. Caption-vs-CTC(truth) divergence:
       scene006 mean +289 ms, MAX 1270 ms (~4-word lag on the loanword-dense scene);
       scene002 max 455 ms; scene003 max 343 ms (drifts even though count coincidentally matched).
   (c) scene006 (~37-40s) and scene002 (~9-12s) are NON-1:1 (whisper 23 vs 22 toks; 13 vs 12) → the
       interpolation fallback drifts. CONFIRMED root cause = whisper alignment on loanwords.

3. "engineering"/"tồn tại" drag: MEASURED ms/syllable (CTC) in scene003: engineering 165 ms/syl (NOT
   slower than neighbours ~140-200), tồn 200 / tại 220 (mildly slow, within band). The audio is
   CONTINUOUS VOICED (no internal silence gap). => This is F5-native VOICED vowel elongation, NOT dead
   air. Post-processing (silence removal) CANNOT fix it. Only seed-selection/re-roll (or a per-word
   atempo speed-up, which the owner's constant-pace rule discourages) addresses it. HONEST: not fixable
   by the gap shaper or CTC trim.

4. "năm | 2026" front gap = 280 ms (CTC năm→year). WHY the shaper missed it: (i) energy — the 280 ms is
   ~50 ms voiced + ~200 ms breath at -40..-52 dB, fragmented by the -40 dB threshold into 55+60 ms
   sub-runs, never seen as one removable run; (ii) whisper — the shaper's DP maps whisper words to
   narration tokens, but the narration token is the DIGIT "2026" while whisper transcribes "2026" as one
   token and F5 SPEAKS it as 4 syllables ("hai không hai sáu"); the digit-token ↔ 4-spoken-syllable
   mismatch means the shaper cannot cleanly bracket the "năm|year" junction. CTC (with `_normalize_years`)
   aligns "năm"→"hai"(280ms) correctly. => CONFIRMS hypothesis: whisper can't classify this junction; CTC can.

5. bờ~rôm: whisper "bờ"(2.04-2.34)→"rôm"(2.34-2.60), gap_before = 0 ms (FLUSH). Energy gaps in scene002 =
   (none). The inter-syllable SILENCE is already fully removed. What remains is pure syllable ARTICULATION
   (the /b/,/ɤ/,/r/,/o/,/m/ phonemes themselves) — there is NO silence left to cut. Reducing "another 50%"
   requires FUSING the two syllables (atempo-compress the whole "bờ rôm" unit, or shorten the /ɤ/ vowel),
   NOT gap removal. Decisive option: treat "pờ~rôm" as ONE atempo>1 unit (compress the 2-syllable region
   ~10-15%) so it reads as a single tight word — but that speeds those syllables (mild constant-pace
   deviation, already allowed for say_as terms).

6. "tùy | chỉnh" = 201 ms (CTC). Same as #4: energy-only shaper saw just the ~80 ms sub-(-40dB) tail and
   floored it, leaving the ~120 ms voiced glide-tail of "tùy" as an audible beat. whisper here actually
   transcribed "tuy chỉnh" (count matched), so the failure is the ENERGY layer, not whisper — but a
   CTC-driven word-boundary compressor would collapse the full 201 ms inter-word region. CONFIRMED same root.

## Would switching gap-shaper + karaoke from whisper→CTC fix items 1/2/4/6?

- Item 2 (karaoke): YES — CTC known-text alignment gives accurate per-word onsets on loanword scenes
  where whisper count-mismatches and drifts (scene006 max 1270ms → CTC ~0). Highest-confidence win.
- Items 4 & 6 (mid-phrase beats at word junctions): YES — a CTC-word-boundary-aware inter-word
  compressor removes the VOICED-tail+breath region (201/280 ms) the energy shaper structurally cannot.
  This is the durable general fix the owner has wanted (works regardless of where F5 puts the beat).
- Item 1 (period beat too long): NO — not alignment; it's the intended CF_PS_GAP_PARA_S beat being too
  long for taste. Fix = lower that knob / punct-beat target.
- Item 3 (voiced drag): NO — F5-native voiced stretch, not silence. Only seed-selection/re-roll or a
  per-word atempo can touch it; honestly out of reach for any alignment/silence method.
- Item 5 (bờ~rôm): NO silence left; needs a fuse/atempo on the syllable pair, not alignment.

## Concrete implementation plan (per item) — NOT YET IMPLEMENTED

1. Add knob to shorten sentence-period beat: lower CF_PS_GAP_PARA_S (0.22→~0.14) and/or CF_PS_PUNCT_MAX_S
   (0.34→~0.24) so period beats read shorter. Cheap, pure-knob, no alignment. Verify by ear + ffprobe.
2 & 4 & 6: Replace the gap-shaper's whisper DP with CTC word boundaries (known text, `_normalize_years`),
   and switch the karaoke word_map to CTC per-word timestamps (the aligner is already installed, CPU,
   offline). Compress the FULL inter-word region (voiced tail + breath) between bound (no-punct) word
   pairs to a small floor; keep punct beats. This unifies items 2/4/6 under one CTC mechanism and is the
   durable general fix. Risk: CTC adds ~1 CPU pass/scene (already used for item2 trim) — acceptable.
3: Seed-selection / re-roll for voiced drag (loanword rush repair infra exists: F5_LOANWORD_REPAIR).
   Optionally a bounded per-word atempo on a flagged dragged loanword. Flag to owner it's a re-roll lever.
5: Make "pờ~rôm" a single atempo>1 unit (~0.88) so the 2 syllables fuse ~12% tighter; measure before/after.
   No silence left to cut.

NOTE: all evidence from the ACTUAL job-122 audio (render 118), api.log, and CTC-vs-whisper alignment
printouts above. No code changed in this task.

## IMPLEMENTED (CTC-backbone durable fix, 2026-07-05)

Flag: CF_ALIGN_BACKEND=ctc (default; set =whisper to revert). Per-scene fail-safe to whisper/energy.

Dashboard/api/workers/tts_worker.py
- CF_PS_GAP_PARA_S 0.22->0.14, CF_PS_PUNCT_MAX_S 0.34->0.24 (item 1 knobs).
- NEW _ctc_align_words(): shared CTC per-word [(word,start,end)] primitive (known text, year-normalized,
  MMS_FA + native forced_align). _ctc_last_word_end() now wraps it.
- NEW _shape_nonpunct_gaps_by_ctc(): CTC-word-boundary gap shaper — compresses the region STRICTLY
  between word-END(prev) and word-START(next) for no-punct pairs (the voiced glide+breath the energy
  pass missed), keeps/caps punct beats. CF_CTC_EDGE_S=0.012 guard. Returns None -> energy fallback.
- _synth_scene_per_sentence: try CTC shaper, fall back to energy per-scene, log backend used.

Dashboard/api/workers/whisper_worker.py
- NEW _ctc_align_item() + align="ctc" branch: karaoke word_map via CTC known-text alignment; per-item
  whisper fallback; logs backend per scene.

Dashboard/api/generate.py
- assemble_footage caption word_map: passes align=CF_ALIGN_BACKEND(ctc) + per-scene narration(=caption).

VERIFY (fresh F5 re-synth through the real CTC-backbone pipeline; all scenes logged "used ctc backend"):
- Item 6 tùy|chỉnh: 201ms -> 60ms. Item 4 năm|2026: 280ms -> 20ms. Item 1 period thể.|Người capped ~241ms
  (was 265-360). Word tails intact (scene46 "lặp", scene1 "gì", scene9 "nào", scene3 "2026").
- Karaoke: scene2 whisper max 455ms -> CTC 0ms (12==12 exact); scene6 whisper max 1270ms -> CTC 0ms
  (22==22 exact). KARAOKE_GAP_RUSH stays 0.
- Item 5 (bờ~rôm) and item 3 (drag) NOT touched per owner.
