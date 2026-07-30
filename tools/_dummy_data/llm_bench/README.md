# `tools/_dummy_data/llm_bench` — bench fixtures (dummy data, NOT source)

Everything in this folder is **input fixture data for `tools/llm_bench.py`**. It is
deliberately isolated here (project convention: dummy/seed/fixture data lives in a
dedicated `_dummy_data/` dir, never mixed into source).

Nothing here is imported by the production pipeline. `tools/llm_bench.py` does **not**
import `Dashboard/api/generate.py`; the prompt text and the expected scene-JSON shape
below are **copies captured from `generate.py` on 2026-07-30**, so the harness stands
alone. If `generate.py`'s prompts change materially, re-capture these files — they will
not update themselves.

## Layout

```
tasks/       one *.task.json manifest + one *.prompt.txt per benchmark task
schemas/     the response_format json_schema sent when structured output is enabled
responses/   saved provider responses used by `--dry-run` (parsing/scoring, no network)
```

## Tasks

| Task id | What it measures | Shape |
|---|---|---|
| `topic_gamestory_60s` | short-input / short-output leg: topic → 9 scenes, ~117 VN words | `scene`, `narration`, `image_prompt` |
| `footage_recap_600s` | long-input / long-output leg: 10-min source transcript → RECAP script | `scene`, `narration`, `sourceStart`, `sourceEnd` |

`footage_recap_600s` is the leg that actually matters for provider selection — it is the
one that stresses context length, instruction adherence over many scenes, and the word
budget. `topic_gamestory_60s` is the cheap smoke leg.

## Where the numbers came from

The budgets baked into the prompts and the `expect` blocks were derived with
`generate.py`'s constants as of 2026-07-30 and then **frozen** into the fixture:

- `_VI_WORDS_PER_SEC = 2.2`, `_DURATION_SAFETY = 0.90`, `_CREDIT_SLATE_SEC = 3.0`,
  `CF_OUTRO_CARD_SEC = 3.0`, outro CTA = 15 words.
- FIXED budget: `round(duration * 2.2) - 15` → 60 s ⇒ `round(132) - 15 = 117`.
- AUTO ceiling: `round((source - slate - outro_card) * 0.90 * 2.2) - 15` →
  600 s ⇒ `round(594 * 1.98) - 15 = 1176 - 15 = 1161`.

The scorer compares against the number written **inside the prompt text**, so the fixture
is self-consistent whatever `generate.py` does later.

## Swapping in a real captured prompt

The transcript in `footage_recap_600s.prompt.txt` is synthetic (written for this fixture,
no real source video). To bench against a real job instead, either edit that
`.prompt.txt` in place or run:

```
python tools\llm_bench.py --prompt-file <path-to-your-prompt.txt> --shape footage \
    --expect-scenes 40 --expect-words 1161 --expect-duration 600
```
