# Script fixes — video id=2 ("Giải Thích Mọi Thứ", F5-TTS clone, edit_mode=summary)

Source: `_workspace/video2_script.json` (57 scenes)
Output: `_workspace/video2_script_fixed.json` (only `narration` strings changed; `scene`/`sourceStart`/`sourceEnd` untouched, verified by diff).

## Conventions adopted

F5-TTS has no SSML. Prosody is driven ONLY by punctuation and spelling. Choices:

- **Acronyms that F5 rushes (MCP, RAG)** → respelled as Vietnamese per-syllable phonetics so the engine reads them as a word it can pronounce rather than rushing the bare Latin letters:
  - **MCP → `em-xê-pê`** (letters M, C, P spelled in Vietnamese; hyphens keep them as one pronounceable unit).
  - **RAG → `rát`** (read as the Vietnamese-style single syllable the source narrator uses, instead of three rushed consonants). This is the common spoken form for "RAG" in Vietnamese tech talks.
  - A comma is placed **immediately after** the respelled acronym so it cannot collide with the following word.
  - ⚠️ **NEEDS A TTS SPOT-CHECK.** I cannot guarantee F5 pronounces `em-xê-pê` or `rát` correctly without hearing it. If the spot-check fails, fallback options to try, in order: (a) spaced caps `M C P`; (b) periods `M.C.P.`; (c) tune the hyphenation (`em xê pê`). Do not ship blind.
- **Brand / product names (Cursor, Windsurf, Cline, Roo, Aider, ChatGPT, Slack, Anthropic, Klein, Ralph, RAL, ColPali, Claude)** → kept in **original spelling**, never Vietnamese-ized. To stop F5 rattling a list, the 5-brand list (scene 13) uses `...` (ellipsis) for a longer pause and the comma separators are retained.
- **Established English tech terms** (agent, context window, token, tool calling, sub-agent, swarm, scope, bug fix, coding agents, prompt/context/harness engineering, loops, pull request, PR, system prompt, orchestration layer, execution environment, context management, requirement, JSON, feature, lightweight) → **left in English**, unchanged. None were translated.
- **Numbers** → Vietnamese thousands separator: `4,000` → `4.000` (English comma reads as a decimal/odd pause to a Vietnamese checkpoint; `12 tiếng` left as-is, it reads fine).
- **Pacing punctuation** → swapped em-dashes (`—`) that fell mid-phrase for commas at real clause boundaries; F5 ignores `—` for prosody and was fragmenting long noun phrases. Light touch only (one breath point per long clause), to avoid choppy robotic delivery.

## Change table

| Scene | Bug# | Before | After | Why |
|---|---|---|---|---|
| 5 | 8 + #6(number) | `...chỉ có 4,000 token — quá nhỏ để làm gì đáng kể.` | `...chỉ có 4.000 token, quá nhỏ để làm gì đáng kể.` | `4,000`→`4.000` (VN thousands sep, avoids decimal misread); `—`→`,` gives a real breath point after the number+term cluster. |
| 8 | 1 + 8 | `...dùng tool calling, MCP, và RAG để quản lý context window tốt hơn.` | `...dùng tool calling, em-xê-pê, và rát, để quản lý context window tốt hơn.` | Three acronyms in one sentence; MCP/RAG respelled phonetically + comma after `rát` so the trailing clause doesn't collide. ⚠️ spot-check. |
| 10 | 1 | `MCP thêm tính năng đặc thù của vendor vào model.` | `Em-xê-pê, thêm tính năng đặc thù của vendor vào model.` | MCP rushed and ran into next word; respell + comma forces a clean pause. ⚠️ spot-check. |
| 11 | 1 + 2 | `RAG kết nối cơ sở dữ liệu tùy chỉnh luôn sẵn có.` | `Rát, kết nối cơ sở dữ liệu tùy chỉnh, luôn sẵn có.` | RAG respelled; comma after the long noun phrase `cơ sở dữ liệu tùy chỉnh` gives F5 a legitimate breath point so it stops breaking inside `tùy chỉnh`. ⚠️ spot-check. |
| 13 | 5 + 8 | `Cursor, Windsurf, Cline, Roo, Aider — dùng tool calling cho context engineering.` | `Cursor, Windsurf, Cline, Roo, Aider... đều dùng tool calling cho context engineering.` | 5 brand names rattled too fast; `—`→`...` for a longer pause before the verb, brands kept original spelling. Added `đều` for natural flow after the list. |
| 25 | 8 | `Task kéo 12 tiếng — context window đầy thì agent tóm tắt lại rồi tiếp tục.` | `Task kéo 12 tiếng, context window đầy thì agent tóm tắt lại rồi tiếp tục.` | `—`→`,` at the clause boundary so the number+term opener gets a clean breath. |
| 31 | 4 | `...để quản lý context theo tầng bậc, hoặc triển khai swarm...` | `...để quản lý context theo bậc, hoặc triển khai swarm...` | Requested wording fix: `theo tầng bậc` → `theo bậc`. |
| 32 | 8 | `...harness agent — cần orchestration layer, execution environment và context management tốt hơn.` | `...harness agent, cần orchestration layer, execution environment, và context management tốt hơn.` | `—`→`,`; added comma before `và` in a 3-item English-term list so F5 breathes between stacked terms. |
| 36 | 8 | `...bạn không cần sửa thủ công — chỉ cần spawn một agent...` | `...bạn không cần sửa thủ công, chỉ cần spawn một agent...` | Long sentence; `—`→`,` for a natural mid-clause pause. |
| 38 | 8 | `...đóng máy hoàn toàn — agent vẫn chạy trên cloud...` | `...đóng máy hoàn toàn, agent vẫn chạy trên cloud...` | `—`→`,` pacing on a long sentence. |
| 40 | 8 | `...vận hành hoàn toàn tự động — agent tự kiểm tra model mới mỗi ngày...` | `...vận hành hoàn toàn tự động, agent tự kiểm tra model mới mỗi ngày...` | `—`→`,` pacing on a long sentence. |
| 42 | 8 | `Agent chạy trong vòng lặp — mỗi vòng có context mới...` | `Agent chạy trong vòng lặp, mỗi vòng có context mới...` | `—`→`,` pacing on a long sentence. |
| 44 | 8 | `Ví dụ tiêu biểu là RAL — lan truyền mạnh nhờ hiệu quả cao...` | `Ví dụ tiêu biểu là RAL, lan truyền mạnh nhờ hiệu quả cao...` | `—`→`,` pacing. RAL kept as original (proper noun); not respelled — flag below. |
| 48 | 8 | `...persona cho agent — nhắc nó biết mình là coding agent.` | `...persona cho agent, nhắc nó biết mình là coding agent.` | `—`→`,` pacing. |
| 49 | 3 | `...Harness Engineering tận dụng cả hai — đây là sự thay đổi mô hình.` | `...Harness Engineering tận dụng cả hai, đây là sự thay đổi mô hình.` | Kept the punchline `đây là sự thay đổi mô hình` here (per instruction); `—`→`,` for pacing. |
| 50 | 3 | `Đây là sự thay đổi mô hình — agent hoạt động trong chuỗi bước có cấu trúc.` | `Agent hoạt động trong chuỗi bước có cấu trúc.` | Removed the duplicated opening phrase (it now lives only at end of scene 49); scene 50 starts directly on the new idea. Reads smoothly back-to-back. |
| 54 | 8 | `...theo mô hình này — tạo requirement document, xuất JSON, rồi loop từng feature.` | `...theo mô hình này: tạo requirement document, xuất JSON, rồi loop từng feature.` | `—`→`:` introduces the enumerated list naturally; commas already break the 3 stacked terms. |
| 55 | 8 | `Kiến trúc này rất nhỏ gọn — repo Ralph và demo Anthropic đều lightweight, đơn giản.` | `Kiến trúc này rất nhỏ gọn, repo Ralph và demo Anthropic đều lightweight, đơn giản.` | `—`→`,` pacing; brands Ralph/Anthropic kept original. |
| 57 | 8 | `...nói về harnessing layer — vì nó thực sự hiệu quả.` | `...nói về harnessing layer, vì nó thực sự hiệu quả.` | `—`→`,` pacing. |

## Which scenes were touched for which reason

- **Targeted bug fixes (the located bugs):**
  - Bug 1 (MCP/RAG rushed): scenes **8, 10, 11**
  - Bug 2 (mid-word break in `tùy chỉnh` / `kỷ nguyên`): scene **11** (re-punctuated). Note: scene 12 (`kỷ nguyên mới`) was **left unchanged** — see flag below.
  - Bug 3 (duplicate `đây là sự thay đổi mô hình`): scenes **49, 50**
  - Bug 4 (`theo tầng bậc`→`theo bậc`): scene **31**
  - Bug 5/6 (brand list, no translation): scene **13**
- **Pacing-only (Bug 8, em-dash→comma / number formatting, no semantic change):** scenes **5, 25, 32, 36, 38, 40, 42, 44, 48, 54, 55, 57**

## Uncertainty flags (honesty)

1. **`em-xê-pê` (MCP) and `rát` (RAG) — NEEDS A TTS SPOT-CHECK.** This is the highest-risk change. I have not run F5; I cannot confirm these respellings pronounce correctly. Render scenes 8, 10, 11 first and listen before committing the full batch. Fallback ladder documented in Conventions.
2. **Scene 12 (`kỷ nguyên mới`) left UNCHANGED.** Bug 2 reported a mid-word break in `kỷ nguyên`. The original sentence (`Ba kỹ thuật này khai sinh kỷ nguyên mới của coding agents.`) is short and already has no separator inside `kỷ nguyên`, so there is no obvious text edit that fixes a mid-word break — that fragmentation is likely an F5 inference artifact, not punctuation-driven. Adding commas around such a short clause would make it choppy. **Recommendation:** re-render scene 12 as-is and re-listen; if it still breaks, this is a model-level artifact for media-engineer (ref-clip / chunking), not a script fix. Flagging rather than pretending an edit fixes it.
3. **`RAL` (scene 44) and other one-off proper nouns** (Klein, ColPali if present, Ralph) left in original spelling, NOT respelled. They were not in the rushed-acronym bug report, so I did not phoneticize them. If any sounds wrong on render, treat the same as MCP/RAG.
4. The number `12 tiếng` (scene 25) was left as digits — Vietnamese TTS generally reads `12` fine; only the comma-separated `4,000` was reformatted. Confirm on spot-check.

## Re-render list

**Scenes whose narration changed and therefore need TTS re-render (19):**

`5, 8, 10, 11, 13, 25, 31, 32, 36, 38, 40, 42, 44, 48, 49, 50, 54, 55, 57`

All other scenes are byte-identical and can reuse existing audio.
