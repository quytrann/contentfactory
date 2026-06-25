---
name: media-models
description: >-
  How to install, run, and debug the ContentFactory model/media stack on the
  local 8GB-VRAM machine: ComfyUI+SDXL image gen, TTS & voice-clone (VieNeu,
  F5-TTS + ViVoice Vietnamese checkpoint), faster-whisper, FFmpeg assembly
  (Ken Burns/captions/bgm), and stickman renderers (procedural 2D, Blender
  headless). Use for model setup, inference commands, GPU/VRAM fit, render
  debugging, audio/video generation. Triggers on "TTS", "clone giọng",
  "F5-TTS/ViVoice/VieNeu", "ComfyUI/SDXL", "whisper", "FFmpeg", "stickman",
  "Blender", "render", "model", "VRAM".
---

# Media & model stack

Used by **media-engineer**.

## Environment facts
- Tools under `E:\Installed\<Tool>` (never `C:\Program Files`): `cf-venv` (content models + CUDA torch), `FFmpeg`, `Blender`, `f5-vietnamese\ViVoice`.
- **RTX 2070 8GB, models run SEQUENTIALLY.** Disk/shared mem ≠ VRAM. Choose models that fit (SDXL, not full Flux).
- cf-venv python: `E:\Installed\cf-venv\Scripts\python.exe`. Workers invoked via `_run_cf_worker`.

## TTS / voice clone (F5-TTS + ViVoice) — the verified recipe
- Engine in cf-venv (CUDA torch). Checkpoint: `E:\Installed\f5-vietnamese\ViVoice\{model_last.pt,vocab.txt}`.
- Run: `python -m f5_tts.infer.infer_cli --model F5TTS_Base --ckpt_file <pt> --vocab_file <vocab> --vocoder_name vocos --ref_audio <wav> --ref_text "<exact transcript, lowercase>" --gen_text "<lowercase vi>" --output_dir <dir> --output_file <name>.wav`.
- **Gotchas (must do):** `PYTHONUTF8=1`; FFmpeg on PATH; `torchcodec` must NOT be installed (needs FFmpeg ≤7); `f5_tts/model/__init__.py` Trainer import lazy (else datasets→pyarrow crash); **always pass explicit `--ref_text`** (transcribe the ref with faster-whisper first — F5 auto-ASR mis-transcribes → garbled); lowercase gen_text; avoid raw Latin abbreviations in text (TTS mispronounces).
- Base `F5TTS_v1_Base` can't do Vietnamese (garbled) → ViVoice only.
- **Objective verification (required):** transcribe the generated wav with faster-whisper and compare to intended text before calling it good. Also ffprobe duration/codec.

## Images / assembly / stickman
- ComfyUI+SDXL via `generate.py` workflow (KSampler→VAEDecode→SaveImage); FFmpeg Ken Burns + captions + bgm.
- Stickman: procedural-2D = PIL skeleton keyframes → frames → FFmpeg (CPU, 0 VRAM); Blender = headless `blender -b -P script.py`, **Z-up** (convert Y-up skeleton math).

## Coordination
- Give backend-engineer the exact CLI/env/payload to wire. Model availability/size/version → researcher. GB-scale downloads → surface size to leader for user confirmation; check HF cache before re-downloading.
- Notes → `_workspace/`; finished media → `E:\ContentFactory\<page>`; sample/dummy clips in a dedicated samples dir.
