# How to edit video (transformation playbook for translate/reup pages)

This page produces Vietnamese reup/translate content. To stay monetizable and
avoid plain reuploads, every video should **transform** the source, not just
re-voice it. Pick one of the editing modes below, then apply the safety
formula in section 6.

The modes are: **Commentary**, **Recap**, **Educational**, **Summary**,
**Dubbed**, and **Translate-full**. The first four are genuine-transformation
modes. **Dubbed** and **Translate-full** are high-risk, owner-accepted
exceptions that keep 100% of the source content — see sections 5 and 6. Neither
is a default, and both are the closest modes to a plain reupload: Dubbed keeps
the original audio + subtitles only, while Translate-full re-voices the source in
**natural** Vietnamese (voiceover + subtitles), keeping **100% of its content**
and cutting only junk — a full localization, not a genuine transform.

## 1. Commentary (analysis + opinion)

**Idea** — Don't just translate; you must:
- explain
- give your take
- state a personal point of view
- analyze in depth

**Example** — Instead of reuping a "drama / news / interview" video, make:
- "Why this person is so controversial"
- "3 other angles on this event"
- "What the original video didn't make clear"

**How to make it monetizable:**
- Your own presence or voiceover.
- Original footage is illustration only (≤ 20–40%).
- Your narration is the main content.

## 2. Recap (valuable summary)

**Idea** — Watch long content → summarize and retell selectively.

**Easy formats:** films / series, anime, podcasts, long-form news.

**Examples:**
- "10-minute recap of film X, but explaining the meaning of the ending"
- "Full story of drama Y + an easy-to-follow timeline"
- "Details you missed in the original video"

**Important** — Don't just "copy the content." You must:
- condense (keep roughly **60–75%** of the source — drop the least-important beats)
- re-order the logic (don't just follow the source order)
- add a **light** layer of analysis (open with a strong hook, close with a takeaway/lesson)

## 3. Educational (teach / explain)

**Idea** — Turn the content into knowledge, lessons, or a how-to.

**Examples** — If the original is a success story, a drama, or a viral clip,
turn it into:
- "The lesson to take from this story"
- "The psychology behind this behavior"
- "Why did this phenomenon go viral?"

## 4. Summary (condense the original) — `summary` mode

**Idea** — Like **Recap**, but **keep more of the original content** (keep roughly
**76–90%** of the source). Follow the source's own **chronological** structure and
sequence; only **trim filler** — the redundant, repetitive, or unnecessary stretches.
Then translate and **retell it faithfully in your own narrative/storytelling voice** —
condensed and smooth, never a verbatim translation, and without injecting your own
take/opinion.

**How it differs from Recap** — Recap is aggressive: it re-orders the logic, cuts
heavily, and layers in analysis/opinion/lessons. Summary is lighter: it keeps a
**higher share of the original footage**, preserves the source's flow, and mainly
removes the dead weight. Think "the same video, just shorter, cleaner, faster" rather
than "a new take on the video."

**How to build (this is the `summary` edit mode in the Studio "Tạo video" form):**
- Ingest + transcribe the full source; walk its beats in their **original order**.
- Drop only the redundant / filler / off-topic stretches.
- Rewrite the rest concisely as **your own** narration — your storytelling voice,
  condensed, never word-for-word.
- Original footage is the main visual, but **your Vietnamese narration leads** and is
  the primary content (it carries the meaning; the footage illustrates it).
- Fit the result to the chosen **target length** (the Studio 1–50 minute slider).
- Pairs naturally with the **"Giữ nguyên video gốc — chỉ cắt"** render engine
  (keep the source visuals, just trim to the target length), or with AI images
  if you want fresh visuals over the summary narration.

**When to use** — Long videos, podcasts, news, or streams the viewer just wants
the gist of, quickly, without losing the original's shape.

**Safety** — Summary still obeys the **same formula** as the other modes (section 6):
real transformation (own-voice retelling + trimming, not a 1:1 translation), your voice
as the main content, original footage as illustration, and **no verbatim reupload**.
Because it keeps more original than Recap, be extra careful not to let it slide into a
plain "translate + trim" reupload — the narration must genuinely be your own retelling.

## 5. Mode 5: Dubbed (high risk, owner-accepted exception)

**Idea** — Keep the original audio AND video; do NOT re-voice it. Trim only filler
(logos, ads, intro/outro, dead air, sponsor reads), then burn Vietnamese subtitles
translated from the source transcript over the original footage.

**What it does (and does not) do:**
- KEEPS the original A/V — the source's own picture and the source's own spoken audio.
- TRIMS only filler segments (the same non-content cuts the other modes drop).
- BURNS Vietnamese subtitles translated faithfully from the source (closer to literal
  than a narration — the subtitle must match the spoken line).
- Uses NO TTS, NO AI images, NO stickman — there is no Vietnamese voiceover at all.

**Why it is flagged high-risk** — Dubbed is NOT covered by the ">60-80% your own
voice" transformation safety formula in the section above. There is no original
narration and no real re-editing of the content: it is essentially the source video,
trimmed, with translated subtitles. It is therefore the closest mode to a plain
reupload and carries the highest copyright risk of all modes.

**Status** — This is an explicit, owner-accepted exception (China reup -> Facebook).
It is allowed only because the owner accepted that risk deliberately; it is not a
default and must not be used as a substitute for genuine transformation when another
mode would do. Source credit at the end of the video is mandatory (source_name /
source_link), and the creator/credit fields belong to the project owner
(TODO_ASK_USER if unknown), never inferred from the logged-in account.

**Monetization** — Not asserted here. Whether a trimmed-and-subtitled reupload is
monetization-eligible on any platform is region- and time-dependent and must be
verified with the researcher before any such claim is made.

## 6. Mode 6: Translate-full (high risk, owner-accepted exception) — `translate_full` mode

**Idea** — A **full localization** of the source: re-tell the **entire** video in
**natural spoken Vietnamese**, keeping **100% of the substantive content** (every
point, fact, and example, in the original order) but **cutting the junk** (ads,
sponsor reads, the source's own credit/attribution/watermark call-outs, bloated
intros/outros). Keep the source **video** on screen (audio muted), speak the
Vietnamese narration over it, and burn Vietnamese subtitles from the same text.

> **Design note (changed 2026-07):** Translate-full used to do a **1:1 literal,
> time-locked** translation — one Vietnamese line per source segment, each pinned
> to that segment's time window. That forced an **unnatural, slow** read (the voice
> had to stretch or rush to fit each source segment). It now generates the script
> the **same way `summary`/footage does** — natural, comfortably-paced Vietnamese
> narration in free-standing scenes with their own source windows — **but keeps ALL
> the content** instead of condensing. It is no longer literal and no longer
> time-locked to the source segments.

**What it does (and does not) do:**
- Generates a **natural-narration** script (like Summary), NOT a literal
  line-by-line translation and NOT locked to each source segment's timing — the raw
  transcript's choppy phrasing is rewritten into smooth, comfortably-paced Vietnamese.
- Keeps **100% of the substantive content** — comprehensive coverage of every point,
  fact, argument, example, and number, in the **original order**. This full retention
  is exactly what separates it from **Summary/Recap**, which *condense* and (for
  Recap) *re-sequence*. Translate-full never drops or compresses real content.
- **Cuts only junk** — the narration (and therefore the scenes' source windows) skips
  ads, sponsor reads, like/subscribe/follow prompts, the original channel's
  self-promotion, source **credit / attribution / watermark** segments, and bloated or
  repeated intros/outros. It never cuts real content.
- Keeps the source **video** on screen (source's own picture), **mutes** the source
  audio, produces a Vietnamese **voiceover** (F5-TTS), and burns **per-word karaoke**
  Vietnamese subtitles positioned over the cover band (with EasyOCR caption-cover of
  any burned-in source captions and a subtle Ken Burns zoom).
- Pace is a fixed, natural speaking rate — the voice is **never** sped up or crammed
  to fit; length follows the content (all of it) minus the junk.

**How it differs from the safer transform modes** — Commentary / Recap /
Educational / Summary all *reduce or re-angle* the source: they add the creator's own
voice as the main content (> 60–80%), reorder, analyze, or condense, and keep the
original footage as illustration only. Translate-full does **none** of that: it keeps
the whole source video and re-voices **all** of its content in Vietnamese. The only
thing it removes is junk.

**How it differs from Summary** — Summary and Translate-full now build the script the
**same way** (natural narration, free scenes). The difference is **retention**:
Summary *condenses* (keep ~76–90%, trim redundant/slow real content to hit a shorter
length); Translate-full *keeps everything substantive* (100% of the content, only junk
removed). If you want the video shorter than the source, use Summary; if you want the
whole thing localized, use Translate-full.

**How it differs from Dubbed** — Both keep the full source content. Dubbed keeps the
**original spoken audio** (subtitles only, no voiceover, no TTS, no rewriting).
Translate-full **mutes** the audio and adds a natural Vietnamese TTS **voiceover** +
subtitles. Dubbed is closest to "the source with subtitles"; Translate-full is closest
to "the source, re-narrated end to end in natural Vietnamese."

**Why it is flagged high-risk** — Translate-full is **NOT** covered by the
">60-80% your own voice" transformation safety formula in section 7. Making the
narration *natural* does not change that: it still delivers **all** of someone else's
content, in order, over their own footage — a **full localization / reupload with a
Vietnamese soundtrack**, not a transform. It carries the **highest copyright /
reupload risk** alongside Dubbed, and reaches more of the source than any transform
mode because it re-voices the entire thing. Cutting junk does not make it
transformative.

**Status** — Owner-accepted exception only, chosen deliberately per job. It is not a
default and must not stand in for a genuine-transformation mode when Recap / Summary
/ Commentary would serve. Source credit at the end of the video is **mandatory**
(`source_name` / `source_link`); the creator/credit fields belong to the project
owner (`TODO_ASK_USER` if unknown), never inferred from the logged-in account.

**Monetization** — Not asserted here. Whether a full-translation reupload is
monetization-eligible on any platform is region- and time-dependent and must be
verified with the researcher before any such claim is made. Treat it as **at least
as risky as Dubbed** for eligibility purposes.

**When to use** — Only when the owner explicitly wants the entire source delivered
in Vietnamese with a spoken voiceover (not just subtitles), accepts the reupload
risk, and no transform mode is wanted. For anything meant to be safely monetizable,
prefer Recap / Summary / Commentary.

## 7. The "safer" formula (very important)

You should guarantee:

1. **Transformation** — not just translation: analyze, re-order, add a viewpoint.
2. **Your voice is the main thing** — voiceover / facecam / commentary; never let
   the original video become the main content.
3. **Low share of original content** — original footage is illustration only; your
   narration is > 60–80%.
4. **No verbatim reupload** — avoid: re-uploading the full video, only changing the
   language, or only trimming it.

## 8. Niches that are easy to make & monetize

- Drama / internet story (with analysis)
- True crime recap
- Film / anime analysis
- "Reddit story explained"
- Everyday psychology
- Facts / mysteries / pop science

## 9. Example of a good video format

- **Hook (3–5s):** "This story seems simple, but it has a twist that's hard to believe…"
- **Story recap (30–60%):** retell it concisely.
- **Commentary (30–50%):** analysis, takes, lessons.
