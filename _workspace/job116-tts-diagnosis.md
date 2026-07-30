# Job 116 TTS diagnosis (F5 per-sentence path, CF_TTS_PER_SENTENCE=1)

Render dir: `E:\ContentFactory\_cache\renders\116` (49 scenes, mtime 2026-07-05 13:13).
Engine: F5-TTS + ViVoice (default). Active path: `_synth_scene_per_sentence` -> `_synth_sentence_clip`.
Legacy whisper gap-shaper (`_shape_gaps_by_alignment` / CF_GAP_SHAPE) is DISABLED when PER_SENTENCE=1.
Active gap edit = LEVER 1 `_compress_internal_silence_np` with CF_PS_* (energy-only, cap 0.08 -> keep 0.06).

## Objective probe (faster-whisper small + energy silence runs, -40 dB, >=50 ms)

Reported phrase -> scene:
- "định nghĩa | nó" = scene001; "kết nối / tùy chỉnh" = scene009; "đầu năm 2026" = scene003/026;
  "chatGPT 2022" (smooth) = scene004; trailing "ờ" = scene016; "bờ~rôm" (prompt) = scene002.

### Item 1 (mid-phrase beats) — MAPPING IS RELIABLE
Energy run -> bracketing whisper words -> narration punctuation class works:
- scene001 run@5.14+5.21 (65+55ms) BETWEEN 'nghĩa'|'là' = NO punct -> should butt-join.
  run@3.625 (495ms) after 'thể.' = PERIOD -> keep beat. run@6.86 (95ms) after 'agent,' = comma -> small beat.
- scene009 run@0.94 (145ms) BETWEEN 'kết'|'các' = NO punct (bound "kết nối") -> remove. run@2.92 (80ms) mid-clause -> remove.
Whisper reports flush 0ms between adjacent words, so whisper timestamps ALONE cannot localize these beats;
ENERGY detects them and whisper-word brackets classify punctuation. This is the durable signal.

### Item 3 (năm 2026 vs chatGPT 2022) — NOT a chunk-isolation inconsistency
F5_YEAR_INLINE=1 (default): BOTH years are space-joined and read INLINE (no isolated FAST chunk).
Probe: scene003 pre-year gap ~155ms (50+105ms mid-clause), scene004 ~100ms. Both inline; difference is
F5 prosody (soft onset before digits), not a split bug. The pre-year gap is a NO-PUNCT junction ("năm | 2026"),
so item-1's non-punct removal fixes it too. No separate year fix needed.

### Item 2 (trailing "ờ") — it is a MID-CLIP hallucination at a COLON, not a tail
scene016 narration "Kết quả không lý tưởng: website...". Whisper: "...lý tưởng ỔN, website...".
F5 inserted a spurious syllable ("ổn"/"ờ") right before the colon pause (energy run 270ms @1.26s FOLLOWS it).
It is followed by real speech, so trailing-silence trim can't catch it. Needs a targeted extra-token guard
at a clause/colon boundary (whisper token not present in narration).

### Item 4 (bờ~rôm junction) — tighten inactive at factor 1.0
scene002 "prompt"->"pờ~rôm": whisper "Bà Rome", inter-syllable energy gap = 175ms @2.535s.
_F5_SLOW_JOIN_FACTOR=1.0 => in `_synth_sentence_clip` the atempo block is gated `if seg_speed != 1.0`,
so `_tighten_slow_join_gap` NEVER runs. The 175ms is F5 native. Fix: run tighten on tilde segs even at 1.0.
Halve 175ms -> ~85ms target.

### Item 5 (karaoke lag + rush)
- Caption whisper (assemble_footage) runs on the FINAL TTS scene wav (post LEVER 1) -> gap edits ARE reflected.
- `_auto_target_pace` scales audio (atempo) AND word_map timestamps by 1/factor together -> consistent.
- RUSH source = `_aligned_caption_words` lines ~5090-5095 (KARAOKE_GAP_FACTOR=0.30, KARAOKE_GAP_MAX=0.18):
  pulls next word's start EARLIER during a >0.25s gap ("it then leads the audio slightly") = the rush.
- LAG source = whisper marks Vietnamese soft-onset word starts slightly late; EXACT-count 1:1 path uses
  whisper.start directly.
Durable fix: item-1 shaping already runs pre-assembly (captions derive from shaped audio); DISABLE the
forward-pull rush; captions come from whisper on FINAL audio (existing, zero new dependency).

## Fixes implemented (files:lines)

Dashboard/api/workers/tts_worker.py
- NEW `_shape_nonpunct_gaps_by_energy()` (~after line 1957): the general durable fix for items 1+3+4.
  Energy silence runs + whisper-word bracketing + narration-punctuation DP alignment. NO-PUNCT inter-word
  gap -> butt-join to ~CF_PS_NONPUNCT_GAP_S (with 25ms energy edge-guard); PUNCT beat -> kept (clamped to
  CF_PS_PUNCT_MAX_S). New knobs: CF_PS_NONPUNCT_SHAPE(1), CF_PS_NONPUNCT_GAP_S(0.02),
  CF_PS_NONPUNCT_MIN_RUN_S(0.055), CF_PS_PUNCT_MAX_S(0.34), CF_PS_NONPUNCT_THRESH_DB(-40), CF_PS_NONPUNCT_EDGE_S(0.025).
  Called at end of `_synth_scene_per_sentence` on the FINAL 48k scene wav.
- Item 2 (trailing "ờ"/"ổn"/"ợt") FIXED SAFELY via CTC forced alignment (owner-approved).
  REWROTE `_trim_trailing_halluc()` + added `_ctc_aligner()` / `_ctc_last_word_end()`.
  Mechanism: CTC-align the KNOWN narration (year-normalized via `_normalize_years` so digit-word
  years align) -> true end of last REAL word -> trim ONLY a SUSTAINED voiced region after it
  (> CF_PS_TRAIL_VOICED_DB=-20dB for >= CF_PS_TRAIL_VOICED_MIN_S=0.06s = a hallucinated syllable);
  real word decay tail (~-33dB) left intact. CPU-only (CF_CTC_DEVICE=cpu, never GPU). DEFAULT ON.
  Model: torch-hub cache E:\...\.cache\torch\hub\checkpoints\model.pt (1.18 GB), loads OFFLINE.
  Knobs: CF_PS_TRAIL_HALLUC(1), CF_PS_TRAIL_VOICED_DB(-20), CF_PS_TRAIL_VOICED_MIN_S(0.06), CF_CTC_DEVICE(cpu).
  VERIFY: scene16 "ợt" removed / "tưởng" intact; scene46 "lặp" INTACT (regression fixed); years
  scene3/26 "2026" INTACT; full 49-scene sweep fired 16 scenes, ALL retained the real final word
  (160-320ms trims of genuine trailing artifact), 0 real-word clips.
- Install: ctc-forced-aligner pip pkg needs MSVC (C++ ext) - NOT available. Used torchaudio's
  native `forced_align`+`merge_tokens` with the torchaudio MMS_FA bundle (== MMS-300m uroman
  aligner) instead - NO compile, same model, local/CPU/free. Added dep: `unidecode` (uroman-lite).

Dashboard/api/generate.py
- `_aligned_caption_words()` (~line 5089): item 5 RUSH DISABLED. The pull-forward (KARAOKE_GAP_FACTOR/MAX)
  that made the next word pop EARLY during a pause is now gated behind KARAOKE_GAP_RUSH (default 0 = OFF).
  Captions stay on whisper's onset of the FINAL (post-shape, post-pace) audio -> no lead/rush.

## Objective VERIFY on job-116 real audio (production functions applied directly)
- Item 1 scene009 "kết|nối" 145ms->50ms; scene001 "nghĩa|là" 65+55ms->GONE, "rõ|thực" 110ms->GONE;
  period beat after "thể." 495ms->340ms KEPT (clamped); comma beat after "Agile," 95ms KEPT.
- Item 3 scene003 pre-year 105+50ms->50ms (year now inline-tight). Same mechanism, no separate fix.
- Item 4 scene002 "pờ|rôm" 175ms->50ms (71% reduction, > halve); syllables intact (whisper "Quà Rôm").
- Item 2 scene016 clause: "ổn" trimmed (1.44->1.04s), "tưởng" intact; CLEAN clause untouched; scene10
  loanword "tool calling" NOT trimmed (false-positive eliminated); FULL 49-scene sweep fired on 0 scenes.
- Word content preserved (identical whisper transcripts before/after shaping on scenes 1/9/3).
NO .env change required (all knobs are code getenv defaults; worker is a fresh subprocess -> no API restart).
