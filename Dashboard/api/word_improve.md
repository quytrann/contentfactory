# word_improve.md — TTS pronunciation dictionary (F5-TTS / ViVoice + VieNeu + OmniVoice)

Respells English / technical terms so the **spoken audio** sounds right. Applied
ONLY to the text sent to TTS — the caption keeps the ORIGINAL word (viewers HEAR
the fix but READ the correct term). Whole-word, case-insensitive; longest terms
match first. No restart needed: the worker reads this file fresh every run.

Columns: `term` (as written in the script) · `say_as` (respelling fed to F5) ·
`say_as_vieneu` (optional VieNeu-only override; blank = reuse `say_as`, put the
raw term to speak it RAW on VieNeu; ignored by F5) · `say_as_omnivoice` (optional
OmniVoice-only override; blank = leave the term **RAW** on OmniVoice — it does
**NOT** fall back to `say_as`, the critical difference from the VieNeu column;
ignored by F5/VieNeu).

**Two independent readers.** `workers/tts_worker.py` parses the `say_as` /
`say_as_vieneu` columns for F5+VieNeu and **requires a non-empty `say_as`** — a row
with a blank `say_as` is invisible to those two engines. `workers/omnivoice_worker.py`
parses `say_as_omnivoice` separately and requires only that cell. So a row like
`| RAG | | | Rát |` is valid and intentional: OmniVoice says "Rát", F5/VieNeu never see
the term at all. Do **not** "fix" such rows by copying a value into the `say_as` column
— that would add the term to F5's regex _and_ to F5's loanword set (which controls
which chunks F5 re-draws), changing F5 audio.

## Separator semantics (in the `say_as` value)

These apply to the **F5 and VieNeu** paths only. The **OmniVoice** path substitutes
`say_as_omnivoice` as **PLAIN text** — separators/speed markers are NOT applied
(OmniVoice has no atempo path), so an OmniVoice cell should be written as plain
space-joined syllables.

- **hyphen `-`** → the joined syllables read FAST as ONE word, no mid-word gap
  (e.g. `ây-jừn`). This is the default way to bind a term into a single unit.
- **space** → syllables at normal speed with natural boundaries (e.g. `ây jừn`,
  and spelled-out acronyms like `ji pi ti`).

## Entries

| term          | say_as        | say_as_vieneu | say_as_omnivoice |
| ------------- | ------------- | ------------- | ---------------- |
| ChatGPT       | chát ji pi ti |               | chát ji pi ti    |
| OpenAI        | âu pừn ây ai  | OpenAI        |                  |
| sub agent     | sấp-ây-jừn    | sub-agent     |                  |
| agents        | ây-jừn        | agents        |                  |
| agent         | ây-jừn        | agent         |                  |
| system-prompt | xít tờm prôm  | system prompt |                  |
| prompt        | pờ-rôm        | prompt        | prompt           |
| API           | ây pi ai      |               |                  |
| LLM           | eo-eo-em      |               | eo-eo-em         |
| AI            | ây ai         |               |                  |
| CPU           | xi pi diu     |               |                  |
| RAL           | aa ây eo      |               |                  |
| PR            | pi a          |               |                  |
| MCP           | em xi pi      |               |                  |
| NLP           | en eo pi      |               |                  |
| UX            | diu ích       |               |                  |
| UI            | diu ai        |               |                  |
| HTML          | hếch ti em eo |               |                  |
| CSS           | xi ét ét      |               |                  |
| URL           | diu a eo      |               |                  |
| HTTP          | hếch ti ti pi |               |                  |
| SQL           | ét kiu eo     |               |                  |
| TTS           | ti ti ét      |               |                  |
| SDK           | ét đê ca      |               |                  |
| LLM           | eo eo em      |               |                  |
| OCR           | ô xi a        |               |                  |
| ID            | ai đi         |               |                  |
| GPT           | ji pi ti      |               |                  |
| Gemini        | ge mi nai     |               |                  |
| Llama         | eo la ma      |               |                  |
| Cursor        | kơ sờ         |               |                  |
| Tool          | tun           |               |                  |
| website       | wép sai       |               |                  |
| calling       | kon ling      |               |                  |
| RAG           |               |               | Rát              |
| file          |               |               | fai              |
| cloud         |               |               | cờ lau           |
| joule         |               |               | jun              |
| titan         |               |               | ti tan           |
| methane       |               |               | mê tan           |
| K3            |               |               | ka 3             |
| expert        |               |               | és pợt           |
| experts       |               |               | és pợt           |
| AMD           |               |               | ây em đi         |
| XAI           |               |               | ít ây ai         |
| prototype     |               |               | bờ-rồ tô tai     |
| CAP           | ci ây pi      |               | ci-ây-pi         |
