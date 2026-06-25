# word_improve.md — TTS pronunciation dictionary (F5-TTS / ViVoice + VieNeu)

This file fixes how the **spoken audio** pronounces technical / English terms.

The Vietnamese F5-TTS ViVoice checkpoint reads many English tech terms with a
Vietnamese spelling-to-sound mapping, so e.g. `agent` comes out as "ai-gen" and
`ChatGPT` becomes garbled. To fix this we feed F5 a **phonetic respelling** of
the term (a "say_as" value written in Vietnamese letters that, when the VN model
reads it, lands close to the intended English sound).

## Per-engine overrides (F5 vs VieNeu)

The default `say_as` column was tuned and verified on **F5-TTS / ViVoice**. The
pipeline also runs **VieNeu-TTS (ONNX v3turbo)**, a _different_ model whose
spelling-to-sound mapping differs. An F5-tuned respelling does not always land on
VieNeu, and several terms F5 must respell are read CORRECTLY raw by VieNeu
(verified: `agent`, `agents`, `JSON`, `prompt` — the F5 respelling makes them
WORSE on VieNeu).

To handle this without disturbing the F5 column, the table has an OPTIONAL
`say_as_vieneu` column:

- When the TTS engine is **vieneu** and a row's `say_as_vieneu` cell is non-empty,
  that value is used. (To make VieNeu speak the term RAW, put the raw term itself
  in the cell — e.g. `agent`.)
- When `say_as_vieneu` is empty, VieNeu falls back to the default `say_as`.
- For **F5** the `say_as_vieneu` column is **ignored entirely** — F5 behavior is
  byte-identical to before this column existed.

Only fill `say_as_vieneu` for a term you have actually verified differs on VieNeu;
leave it blank otherwise so the shared F5 respelling is reused. Verify the same
way as F5: synth the term on VieNeu (`Vieneu(mode="v3turbo")`) and
whisper-transcribe the wav until it lands on the intended English word.

## How it works (important separation)

- The mapping is applied **ONLY to the text sent to F5-TTS** (the audio).
- The **caption / subtitle keeps the ORIGINAL correct word** (`agent`, `ChatGPT`).
  Captions are built from the script narration, never from this mapped text and
  never from the whisper re-transcription. So viewers HEAR the improved
  pronunciation but READ the correct term.
- Replacement is **whole-word, case-insensitive**, and respects word boundaries
  around Vietnamese text (it will not touch a term embedded inside a longer word).
- Longer terms are matched before shorter ones, so multi-word terms win over
  their parts.

## How to add / edit an entry

Add a row to the table below:

| term | say_as | say_as_vieneu | note |

- `term` — the word as written in the script (what the caption shows).
- `say_as` — the phonetic respelling fed to F5 so it SOUNDS right. Use Vietnamese
  letters and hyphens to shape syllables (e.g. `ây-jừn`). Leave blank
  or omit the row entirely if the term already sounds fine.
- `say_as_vieneu` — OPTIONAL VieNeu-specific respelling. Leave blank to reuse
  `say_as` on VieNeu; put the raw term here to speak it RAW on VieNeu. Ignored by F5.
- `note` — ultra-terse English memo, optional. The parser IGNORES this column, so
  keep just the load-bearing hint: what the RAW (unmapped) term mispronounces to,
  e.g. `RAW→"cha tích"`. Verbose prose is intentionally trimmed.

After editing, no restart is needed — the TTS worker reads this file fresh on
every synthesis run (workers are subprocesses). To verify a new entry, synth a
test line and whisper-transcribe it (see `_workspace/bug_pronun_karaoke_*.md`).

### Tips for choosing a say_as (learned from experiments)

- Spell English letters as VN syllables the model already knows: `G P T` →
  `ji pi ti`, `A I` → `ây ai`, `A P I` → `ây pi ai`.
- For a word, shape the stressed vowel: `agent` → `ây-jừn` was recognized back as
  the English word "agent"; `ây-jân` / `ây-jơ-nt` were not.
- Hyphens help the model keep a respelling as ONE token instead of splitting it.
- Only add a term that TESTING shows is actually mispronounced. Many loanwords
  (`token`, `model`, `harness`, `context`) already read fine on this checkpoint —
  do not "fix" them or you make them worse.

## Entries

Only terms that were verified mispronounced on the ViVoice checkpoint are listed.
Each say_as below was picked by synthesizing candidates and transcribing the
result with faster-whisper to confirm it lands on the intended sound.

The `say_as_vieneu` column is OPTIONAL: blank = reuse `say_as` on VieNeu; a value
= VieNeu-specific override (put the raw term to speak it raw). It is ignored by F5.

| term       | say_as        | say_as_vieneu | note                               |
| ---------- | ------------- | ------------- | ---------------------------------- |
| ChatGPT    | chát ji pi ti |               | RAW→"cha tích"                     |
| agents     | ây-jừn        | agents        | VieNeu RAW; F5 resp→garble         |
| agent      | ây-jừn        | agent         | VieNeu RAW; F5 `ây-jừn`→"ấy kì na" |
| prompt     | prôm          | prompt        | VieNeu RAW; F5 `prôm`→"bờ rôm"     |
| prompting  | prôm ting     | prompting     | VieNeu RAW; F5 `prôm`→"bờ rôm"     |
| API        | ây pi ai      |               | RAW→"ếch"                          |
| AI         | ây ai         |               | RAW→"ế"/"ice"                      |
| CPU        | xi pi diu     |               | RAW→"kíp u"                        |
| RAG        | rát           |               | RAW→"RAG"                          |
| RAL        | a ây eo       |               | RAW→slurred; R=a A=ây L=eo         |
| PR         | pi a          |               | RAW→"PR"                           |
| MCP        | em ci pi      |               | RAW→slurred                        |
| NLP        | en eo pi      |               | RAW→"nấp"                          |
| UX         | diu ích       |               | RAW→"út"                           |
| UI         | diu ai        |               | RAW→VN word "ui"                   |
| HTML       | hếch ti em eo |               | RAW→"thêm"                         |
| CSS        | xi ét ét      |               | RAW→garble                         |
| URL        | diu a eo      |               | RAW→"ôl"                           |
| HTTP       | hếch ti ti pi |               | RAW→"hấp"                          |
| SQL        | ét kiu eo     |               | RAW→"score"                        |
| TTS        | ti ti ét      |               | RAW→"tắt"                          |
| SDK        | ét đê ca      |               | RAW→"xí ghê"                       |
| JSON       | jay sần       | JSON          | VieNeu RAW; F5→"chay xon"          |
| LLM        | eo eo em      |               | weak; RAW→"lâm"                    |
| RAM        | ram           |               | weak; RAW→"giam"                   |
| OCR        | ô xi a        |               | RAW→"OK"                           |
| ID         | ai đi         |               | weak (clashes AI); RAW→"y"         |
| GPT        | ji pi ti      |               | RAW→"GibiBeats"                    |
| Claude     | clót          |               | RAW→"Claude"                       |
| Claud      | cờlau         |               | RAW→"Cloud"                        |
| Gemini     | ge mi nai     |               | RAW→"GAMINI"                       |
| Llama      | eo la ma      |               | RAW→"LÀ MÀ"                        |
| Midjourney | mít jơ ni     |               | RAW→"MITRONY"                      |
| Copilot    | cô pi lốt     |               | RAW→"Copy last"                    |
| DALL-E     | đôn i         |               | RAW→"Zala"                         |
| BERT       | bớt           |               | weak; RAW→"Bird"/"Burst"           |
| debug      | đì bấc        |               | RAW→"demo"                         |
| commit     | com mít       |               | RAW inconsistent                   |
| Git        | gít           |               | RAW→"jit"                          |
| GitHub     | gít hấp       |               | RAW→"ghi phục"                     |
| Viral      | vai rồ        |               | RAW imperfect                      |
| Tool       | tun           |               | RAW imperfect                      |
| Calling    | konling       |               | RAW imperfect                      |

### Verified-fine terms (intentionally NOT mapped)

These read acceptably on the ViVoice checkpoint already — adding a respelling
made them WORSE (or no better) in testing, so they are deliberately left out:

- Already-decided: `token`, `model`, `harness`, `context`.
- Acronyms that read fine RAW: `GPU` (RAW → "GPU").
- Names/words that read fine RAW: `Stable Diffusion`, `Whisper`, `Transformer`,
  `Python`, `JavaScript`, `framework`, `library`, `repository`, `Linux`, `Docker`,
  `terminal`, `script`, `backend`, `frontend`, `server`, `deploy`, `database`,
  `array`, `query`, `code`, `function`, `requirement`.
- `cache` — RAW is imperfect but no tested respelling (`két`, `ksiu`, `card`) landed
  on "cache"; leaving it unmapped rather than adding a worse guess.
