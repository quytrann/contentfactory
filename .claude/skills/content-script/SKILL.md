---
name: content-script
description: >-
  How to design ContentFactory video scripts and editing strategy: the
  Claude-headless script-generation prompts, the editing modes
  (commentary/recap/educational/summary), the transformation playbook
  (how to edit video.md), and copyright/monetization safety for translate/reup
  pages. Use when shaping what a video says, choosing/defining an editing mode,
  writing/adjusting the script prompt, or assessing reupload/copyright risk.
  Triggers on "kịch bản", "script", "cách biên tập", "commentary/recap/
  educational/tóm tắt", "bản quyền", "monetization", "transformation".
---

# Content & script strategy

Used by **content-strategist**.

## Editing modes (keep distinct)
- **Commentary** — translate + analyze + give a take; original footage ≤ 20–40%.
- **Recap** — selective condense & retell; re-order logic, add explanation.
- **Educational** — turn content into a lesson / how-to / explanation.
- **Summary** — condense to key points in the ORIGINAL order, minimal added analysis (lightest transformation; closest to source).
- The full playbook: [how to edit video.md](../../how%20to%20edit%20video.md). When modes overlap, sharpen definitions rather than leaving them ambiguous (recap vs summary differ on order-fidelity + amount of added analysis).

## Safety formula (enforce)
Real transformation; the creator's **own voice > 60–80%** of content; original footage as illustration only; **no verbatim reupload**. Flag risky combos — e.g. *summary + keep-original-footage* drifts toward reupload (`reused content` strike risk). Raise content-policy risks to `leader` with options.

## Script generation
- Prompts feed `claude -p` headless in `generate.py` (`generate_script_transform/footage`): produce scene-by-scene **Vietnamese** narration + image prompts from a transcript or topic. Hand the prompt spec to backend-engineer to encode.
- TTS-safe narration (coordinate with media-engineer): natural Vietnamese, avoid raw Latin abbreviations, lowercase where the checkpoint expects.
- Per-video rule: owner picks the editing mode before a translate workflow; set `videos.source_name`/`source_link`.

## Honesty / facts
- Monetization/platform numbers change by region & time → verify via researcher or label "tham khảo, cần kiểm chứng". Borrowed-account: creator/credit = owner, never inferred (`TODO_ASK_USER`).
- Notes → `_workspace/`; the playbook `.md` is a real deliverable → edit in place (English).
