# Frontend: split the reuse-script flow into two action buttons

## Goal recap
Off a reused-script selection, give the owner two distinct paths:
- **"Dùng lại kịch bản"** — reuse script text only, regenerate TTS fresh (`bypassTtsCache: true`). No warning.
- **"Dùng lại kịch bản và audio"** — reuse script + let the TTS cache serve existing audio (`bypassTtsCache: false`). If the script was edited since the audio was synthesized, show a dismissible warning.

## Files & changes

### `Dashboard/web/src/api.ts`
- Added `bypassTtsCache?: boolean` to `NewJobBody` (the `createJob` request shape), with a comment that it is only meaningful when `reuseScriptVideoId` is set: `true` = force fresh TTS (bypass WAV cache), `false`/omit = let cache serve existing audio. camelCase, matching backend-engineer's contract.
- No change to `types.ts`: the `Job` type does NOT mirror `reuseScriptVideoId` either, so there is no other request/response shape to keep in sync. `NewJobBody` is the single place this field lives.

### `Dashboard/web/src/views/CreateVideo.tsx`
**`ReusableScriptPicker` component:**
- `onPick` signature changed from `(s: ReusableScript) => void` to `(s: ReusableScript, edited: boolean) => void`.
- Added `editedVideoIds: Set<number>` state. `handleSaveEdit` now adds the videoId to that set after a successful inline narration save (cached audio is now stale).
- The "Dùng" button passes `editedVideoIds.has(s.videoId)` as the `edited` flag.

**`Studio` component:**
- New state: `reusedScriptEdited` (bool), `reuseMode` (`'fresh-audio' | 'with-audio'`, default `'fresh-audio'`), derived `bypassTtsCache = reuseMode === 'fresh-audio'`, and `audioMismatchAck` (bool, for "proceed anyway").
- The picker mount's `onPick` now sets `reusedScriptEdited`, resets `reuseMode` to the safe default and clears `audioMismatchAck`. The summary-chip "X" (clear reuse) resets all four.
- `createJob` call now sends `bypassTtsCache: reuseScriptVideoId != null ? bypassTtsCache : undefined` (only attached when a script is actually being reused).
- New UI inside the reused-script summary chip (after the existing cross-mode warning): a two-button mode chooser (the two Vietnamese labels exactly as specified, each with a Vietnamese sub-description and a check mark on the selected one), plus a dismissible amber warning shown only when `reuseMode === 'with-audio' && reusedScriptEdited && !audioMismatchAck`. The warning text is verbatim: "Kịch bản đã được chỉnh sửa — audio cũ sẽ không khớp (cache miss). Nên chọn 'Dùng lại kịch bản' để tạo audio mới." It offers "Chuyển sang 'Dùng lại kịch bản'" (switch to Button 1) and "Vẫn tiếp tục dùng audio cũ" (dismiss + proceed).

## Edited-detection approach (the honest part)
**Chosen: option (a) — session inline-edit tracking.** Studio (CreateVideo) has **no standalone editable script textarea**; the only way to edit a reused script is the inline per-scene narration edit inside `ReusableScriptPicker` (`handleSaveEdit` → `api.updateSceneNarration`). So a literal "compare form field vs videos.script" is not implementable — there is no form field. The faithful signal is: did the user edit this video's narration during this picker session? If yes, the cached WAV no longer matches the script text, which is exactly the "edited since audio was synthesized → cache HIT serves stale audio" condition. That flag is set in `handleSaveEdit` and lifted to Studio via the new `onPick(..., edited)` arg.

### Known limitation (documented, not faked)
This detection only catches edits made **inline in the picker during the current session**. It will NOT flag scripts that were edited in a *previous* session (e.g. the user edited last week, reloaded, and now reuses) or edited through any path other than this picker. A fully reliable signal would require a backend-provided comparison (e.g. `videos` carrying a `script_updated_at` vs an `audio_synthesized_at`, or the reusable-scripts API returning an `audioStale`/`scriptEditedSince` boolean). If the owner wants the warning to fire across sessions, leader should ask the user and request that field from backend-engineer. The current behavior is the safe-by-default subset: the default reuse mode is "fresh audio" (Button 1, always correct), and the warning only ever appears on Button 2 when we *know* an edit happened this session. Picking Button 1 is always safe regardless of detection gaps.

## tsc result
`cd Dashboard/web && npx tsc --noEmit` → **exit code 0** (clean, no errors).
