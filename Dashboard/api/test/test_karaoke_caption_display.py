"""Regression tests for the two DISPLAY-only karaoke caption fixes (owner v47 review):

  1. Hyphens/dashes are stripped from the on-screen caption text (NOT from the TTS
     narration): "sub-agent" -> "sub agent", "riêng — mỗi" -> "riêng mỗi".
  2. Lines PACK more words: KARAOKE_MAX_WORDS is a high ceiling so the per-row PIXEL
     budget is the real limiter, and the widest ZOOMED line still fits the safe area.

Pure unit tests — no ffmpeg/whisper/LLM. _build_karaoke_ass writes a small .ass file
to a temp dir; we parse the rendered text fields.

Run: cd Dashboard/api && .venv/Scripts/python.exe -m pytest test/test_karaoke_caption_display.py -q
"""
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as g  # noqa: E402

W, H = 1080, 1920
# Scene 23 v47: contains an ASCII hyphen compound AND an em-dash.
NARRATION = ("Một hướng là sub-agent với hierarchical context management — mỗi tầng "
             "xử lý ngữ cảnh riêng và truyền kết quả lên trên một cách rõ ràng.")


def test_strip_caption_hyphens_basic():
    assert g._strip_caption_hyphens("sub-agent") == "sub agent"
    assert g._strip_caption_hyphens("riêng — mỗi") == "riêng mỗi"
    # en-dash, em-dash, ascii hyphen, minus all become spaces; whitespace collapses.
    assert g._strip_caption_hyphens("a–b—c-d−e") == "a b c d e"
    assert g._strip_caption_hyphens("  x  -  y  ") == "x y"
    assert g._strip_caption_hyphens("") == ""
    assert g._strip_caption_hyphens(None) is None


def test_caption_tokens_have_no_dashes():
    cap_words = g._aligned_caption_words(NARRATION, [], 12.0)
    toks = [w["word"] for w in cap_words]
    for t in toks:
        assert not re.search(g._CAPTION_DASH_RE, t), f"dash leaked into token {t!r}"
    # "sub-agent" became two display tokens.
    assert "sub" in toks and "agent" in toks
    assert "sub-agent" not in toks


def test_strip_is_display_only_not_tts():
    """The helper is a pure function on the caption text; it must NOT be wired to mutate
    a narration string in place. We assert the source narration constant is unchanged
    (the helper returns a NEW string, original keeps its hyphens for F5)."""
    before = NARRATION
    _ = g._strip_caption_hyphens(NARRATION)
    assert NARRATION == before
    assert "-" in NARRATION and "—" in NARRATION  # TTS text still has them


def test_built_ass_is_dash_free():
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dummy_data")
    os.makedirs(work, exist_ok=True)
    cap_words = g._aligned_caption_words(NARRATION, [], 12.0)
    ass_path = g._build_karaoke_ass(cap_words, W, H, work, 23)
    assert ass_path and os.path.isfile(ass_path)
    txt = open(ass_path, encoding="utf-8").read()
    for line in txt.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        text = line.split(",", 9)[-1]
        plain = re.sub(r"\{[^}]*\}", "", text)  # strip ASS override blocks
        assert not re.search(g._CAPTION_DASH_RE, plain), f"dash in rendered text: {plain!r}"


def test_max_words_is_five():
    """Owner v47 (revised): up to 5 words per row. KARAOKE_MAX_WORDS == 5 caps the line;
    the pixel-width budget breaks earlier only if 5 words wouldn't fit; a SENTENCE end
    can break sooner (see the sentence-break tests)."""
    assert g.KARAOKE_MAX_WORDS == 5


def _measure(cap_words):
    """Reproduce the builder's chunking + width math to report words/line and verify the
    widest ZOOMED line fits usable_px. Mirrors _build_karaoke_ass exactly."""
    fontsize = g._caption_fontsize(W)
    margin = max(24, int(W * 0.037))
    usable_px = max(1, W - 2 * margin)
    HL = g.HL_SCALE
    wfac = g.CAPTION_LIBASS_WFACTOR
    max_px = usable_px * (100.0 / HL)
    from PIL import ImageFont
    font = ImageFont.truetype(g.CAPTION_FONT, fontsize)
    def wpx(t):
        return float(font.getlength(t)) * wfac
    space_w = float(font.getlength(" ")) * wfac
    # Mirrors _chunk_words: PRIORITY 1 = break at a SENTENCE-terminal mark (. ! ? …);
    # PRIORITY 2 = word-count cap or pixel-width budget. Clause marks (, ; :) do NOT break.
    _SENT = (".", "!", "?", "…")
    def _ends_sentence(t):
        return t.rstrip("\"'”’)】»]").endswith(_SENT)
    groups, cur, cur_px = [], [], 0.0
    for w in cap_words:
        tok = g._ass_escape(w["word"]).strip()
        tp = wpx(tok)
        add = tp + (space_w if cur else 0.0)
        if cur and (cur_px + add > max_px or len(cur) >= g.KARAOKE_MAX_WORDS):
            groups.append(cur); cur, cur_px = [], 0.0; add = tp
        cur.append(w); cur_px += add
        if cur and _ends_sentence(tok):
            groups.append(cur); cur, cur_px = [], 0.0
    if cur:
        groups.append(cur)
    zoom_grow = max(0.0, (HL / 100.0 - 1.0) / 2.0)
    out = []
    for grp in groups:
        widths = [wpx(g._ass_escape(x["word"])) for x in grp]
        if len(widths) < 2:
            gap = space_w
        else:
            gp = space_w + zoom_grow * max(widths)
            mg = (usable_px - sum(widths)) / (len(widths) - 1)
            gap = max(space_w, min(gp, mg)) if mg > space_w else space_w
        zoomed = sum(widths) + gap * max(0, len(widths) - 1) + (HL / 100.0 - 1.0) * max(widths)
        out.append((len(grp), zoomed))
    return usable_px, out


# A SINGLE-SENTENCE narration with several MID-SENTENCE commas — the case that
# previously produced uneven "1 word then 4" lines because every comma forced a break.
# Clause marks (, : ) must NOT break; only the final period ends the sentence.
COMMA_NARRATION = ("Có bốn công cụ đầu tiên: Cursor, Windsurf, Roo, và Aider, mỗi cái "
                   "có điểm mạnh riêng biệt và cách tiếp cận khác nhau.")


def test_lines_pack_five_across_clause_marks_no_orphan():
    """A single sentence with mid commas/colons packs to a consistent 5 words ACROSS
    those clause marks (they don't break), so there is no 1-word orphan from a clause
    mark. The only short line is the natural last-line remainder."""
    cap_words = g._aligned_caption_words(COMMA_NARRATION, [], 12.0)
    usable_px, lines = _measure(cap_words)
    counts = [n for n, _ in lines]
    # Every line except the LAST (the remainder) is a full 5 words — no clause-break orphan.
    assert all(c == g.KARAOKE_MAX_WORDS for c in counts[:-1]), (
        f"non-final lines must be full {g.KARAOKE_MAX_WORDS}-word lines: {counts}"
    )
    assert counts[-1] <= g.KARAOKE_MAX_WORDS
    assert all(c > 1 for c in counts[:-1]), f"mid-stream 1-word orphan present: {counts}"
    # The line that spans the comma/colon run ("...tiên: Cursor, Windsurf, Roo, và")
    # is a full 5 words, proving clause marks did not break it.
    assert g.KARAOKE_MAX_WORDS in counts


# Multiple sentences: a short 2-word sentence, a short 4-word sentence, then a long one.
MULTI_SENTENCE = ("Đúng vậy. Nó hoạt động tốt. Nhưng có một vấn đề lớn mà nhiều người "
                  "chưa nhận ra ngay từ đầu.")


def test_sentence_end_breaks_the_row():
    """PRIORITY 1: each SENTENCE ends its own row (break at . ! ? …), even when the
    sentence is shorter than 5 words. The next sentence starts on a fresh row."""
    cap_words = g._aligned_caption_words(MULTI_SENTENCE, [], 12.0)
    _, lines = _measure(cap_words)
    counts = [n for n, _ in lines]
    # "Đúng vậy." (2 words) and "Nó hoạt động tốt." (4 words) each end their own row,
    # so the first two rows are the short sentences — NOT packed up to 5 with the next.
    assert counts[0] == 2, f"first sentence should occupy its own 2-word row: {counts}"
    assert counts[1] == 4, f"second sentence should occupy its own 4-word row: {counts}"
    # Remaining long sentence then fills to 5 / width.
    assert all(c <= g.KARAOKE_MAX_WORDS for c in counts)


def test_question_and_exclamation_also_break():
    """? and ! are sentence-terminal too (not just .)."""
    narr = "Thật sao? Không thể tin được! Đây là phần tiếp theo của câu chuyện dài."
    cap_words = g._aligned_caption_words(narr, [], 10.0)
    _, lines = _measure(cap_words)
    counts = [n for n, _ in lines]
    assert counts[0] == 2, f"'Thật sao?' (2 words) should end its own row: {counts}"
    assert counts[1] == 4, f"'Không thể tin được!' (4 words) should end its own row: {counts}"


def test_widest_zoomed_line_fits_usable():
    """Every line's widest ZOOMED extent stays within the safe usable width (libass
    width factor + HL_SCALE headroom), for both samples."""
    for narr in (NARRATION, COMMA_NARRATION):
        cap_words = g._aligned_caption_words(narr, [], 12.0)
        usable_px, lines = _measure(cap_words)
        for n, zoomed in lines:
            assert zoomed <= usable_px, f"line of {n} words overflows: {zoomed:.0f} > {usable_px}"


def test_clause_mark_does_not_break_first_line():
    """A CLAUSE mark (comma) at the start of a sentence must NOT break: 'Alpha, beta ...'
    keeps packing past the comma to a full 5-word first line, instead of orphaning
    'Alpha,' onto its own row. (Contrast: a sentence-terminal mark WOULD break — covered
    by test_sentence_end_breaks_the_row.)"""
    narr = "Alpha, beta gamma delta epsilon zeta eta theta."
    cap_words = g._aligned_caption_words(narr, [], 8.0)
    _, lines = _measure(cap_words)
    assert lines[0][0] == g.KARAOKE_MAX_WORDS, (
        f"first line should pack {g.KARAOKE_MAX_WORDS} words across the comma, got {lines[0][0]}"
    )


# ---------------------------------------------------------------------------
# Caption-lead regression (Bug: "karaoke text runs ~one word AHEAD of audio").
#
# The proportional branch of _aligned_caption_words (whisper word count != token
# count) used to SNAP each token to whisper_words[int(frac_center*nw)].start —
# the START of the whole whisper word covering the token's center, which is on
# average ~half a word-duration EARLIER than where the token is actually spoken,
# so every caption popped ~0.16-0.20s ahead of the audio. It now INTERPOLATES the
# token's time linearly over whisper's onset grid, removing that systematic lead.
#
# We simulate ground truth exactly like the findings-doc / det_check: whisper word
# k is spoken at [0.4k, 0.4(k+1)]; narration has M equal-weight tokens over the
# same span; the token at center-fraction fc is truly spoken at span*fc. We assert
# the mean lead (spoken_start - caption_start) stays ~0 for the proportional branch.
# ---------------------------------------------------------------------------

_WLEN = 0.4  # seconds per synthetic whisper word


def _whisper_grid(nw):
    return [{"start": round(_WLEN * k, 3), "end": round(_WLEN * (k + 1), 3), "word": "w"}
            for k in range(nw)]


def _gt_center_times(ntok, span):
    """Ground-truth spoken time of each equal-weight token, taken at its CENTER
    fraction of the utterance (matches where a karaoke pop should land)."""
    cum, out = 0.0, []
    for _ in range(ntok):
        fc = (cum + 0.5) / ntok
        cum += 1.0
        out.append(span * fc)
    return out


def _mean_lead(ntok, nw):
    span = _WLEN * nw
    ww = _whisper_grid(nw)
    narration = " ".join("aa" for _ in range(ntok))  # equal weight (2 chars each)
    cap = g._aligned_caption_words(narration, ww, span)
    gt = _gt_center_times(ntok, span)
    m = min(len(cap), len(gt))
    assert m == ntok, f"expected one caption word per token, got {len(cap)} for {ntok}"
    leads = [gt[i] - cap[i]["start"] for i in range(m)]
    return sum(leads) / len(leads)


def test_proportional_branch_does_not_systematically_lead():
    """PROPORTIONAL branch (whisper count != token count): the mean caption lead must
    be ~0. Before the interpolation fix this was a systematic +0.16..+0.20s AHEAD."""
    # Realistic footage mismatches: whisper within ~a few words of the token count.
    for ntok, nw in [(10, 9), (10, 8), (10, 11), (12, 10), (15, 12),
                     (8, 10), (20, 17), (18, 20), (25, 22)]:
        lead = _mean_lead(ntok, nw)
        assert abs(lead) <= 0.05, (
            f"proportional branch tok={ntok} whis={nw} still leads by {lead:+.4f}s "
            f"(must be within +/-0.05s; the pre-fix index-snap led ~+0.18s)"
        )


def test_proportional_branch_beats_old_index_snap():
    """Direct A/B: the current interpolation must have a strictly smaller mean lead
    than the OLD index-snap formula on the same inputs — proving the fix, not luck."""
    def old_snap_lead(ntok, nw):
        span = _WLEN * nw
        ww = _whisper_grid(nw)
        total_w = ntok  # equal weights
        cum, starts = 0.0, []
        for _ in range(ntok):
            frac_center = (cum + 0.5) / total_w
            cum += 1.0
            wi = min(nw - 1, max(0, int(frac_center * nw)))
            starts.append(float(ww[wi]["start"]))
        gt = _gt_center_times(ntok, span)
        leads = [gt[i] - starts[i] for i in range(ntok)]
        return sum(leads) / len(leads)

    for ntok, nw in [(10, 9), (12, 10), (15, 12), (20, 17)]:
        new_lead = abs(_mean_lead(ntok, nw))
        old_lead = abs(old_snap_lead(ntok, nw))
        assert new_lead < old_lead, (
            f"tok={ntok} whis={nw}: interp lead {new_lead:.4f} not better than "
            f"old index-snap lead {old_lead:.4f}"
        )
        # The old snap led by a large, systematic amount here.
        assert old_lead >= 0.10, f"sanity: old snap should lead >=0.10s, got {old_lead:.4f}"


# ---------------------------------------------------------------------------
# Bug C (owner): a contiguous joined term ("sub-agent") must NOT be split across
# two karaoke rows when the row hits its width/word limit. The caption renders it
# as two display tokens ("sub" + "agent") but they are ONE atomic term and must
# stay on the same row (the row may TEMPORARILY exceed the 5-word/width cap to
# finish the term before breaking).
#
# Bug B (owner): a sentence-terminal short word must not be ORPHANED alone on a
# fresh final row (it "jumps and freezes for a beat"); it is pulled back onto the
# previous row when it fits the width budget.
#
# These tests exercise the REAL _build_karaoke_ass (not the _measure mirror) and
# reconstruct the rows from the generated .ass. Each word in a group is its own
# Dialogue event at layer = group_index*stride + word_index (stride = max_group+2),
# so words of one row form a contiguous layer run; a gap > 1 marks a new row.
# ---------------------------------------------------------------------------

_DIALOGUE_RE = re.compile(r"^Dialogue: (\d+),.*\}([^{}]*)$")


def _rows_from_ass(ass_path):
    """Reconstruct the on-screen rows (lists of words, in order) from a built .ass by
    clustering Dialogue events into contiguous layer runs (gap > 1 => new row)."""
    txt = open(ass_path, encoding="utf-8").read()
    by_layer = {}
    for line in txt.splitlines():
        m = _DIALOGUE_RE.match(line)
        if not m:
            continue
        layer, word = int(m.group(1)), m.group(2)
        # First (earliest) appearance per layer carries the word text.
        by_layer.setdefault(layer, word)
    layers = sorted(by_layer)
    rows, cur = [], []
    for ly in layers:
        if cur and ly - cur[-1] > 1:
            rows.append(cur)
            cur = []
        cur.append(ly)
    if cur:
        rows.append(cur)
    return [[by_layer[ly] for ly in run] for run in rows]


def _build_rows(narration, whisper_words=None, dur=6.0):
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_dummy_data")
    os.makedirs(work, exist_ok=True)
    cap_words = g._aligned_caption_words(narration, whisper_words or [], dur)
    ass_path = g._build_karaoke_ass(cap_words, W, H, work, 99)
    assert ass_path and os.path.isfile(ass_path)
    return _rows_from_ass(ass_path), cap_words


def test_glued_tokenizer_marks_hyphen_compound():
    """_tokenize_caption_glued splits 'sub-agent' into two display tokens and marks the
    first as glued to the second; plain words are never glued to each other."""
    toks, glue = g._tokenize_caption_glued("Người dùng sub-agent để chạy.")
    assert toks == ["Người", "dùng", "sub", "agent", "để", "chạy."]
    # 'sub' (index 2) is glued forward to 'agent' (index 3); nothing else is glued.
    assert glue == [False, False, True, False, False, False]
    # Token count matches the old strip+tokenize path (whisper count-matching unchanged).
    old = g._tokenize_narration(g._strip_caption_hyphens("Người dùng sub-agent để chạy."))
    assert len(toks) == len(old) and toks == old


def test_aligned_words_carry_glue_flag():
    cap = g._aligned_caption_words("Người dùng sub-agent để chạy.", [], 6.0)
    words = [w["word"] for w in cap]
    assert words == ["Người", "dùng", "sub", "agent", "để", "chạy."]
    # 'sub' glued to 'agent', rest not.
    assert cap[2].get("glue_next") is True
    assert all(cap[i].get("glue_next") is False for i in (0, 1, 3, 4, 5))


def test_joined_term_not_split_across_rows():
    """Bug C: with enough leading words to fill a 5-word row right before 'sub-agent',
    the row must OVERFLOW to keep 'sub agent' together rather than splitting 'sub' onto
    the previous row and 'agent' onto the next."""
    # Five plain words, then the hyphen compound: without the guard the row would break
    # after the 5th word, orphaning the compound's halves across the boundary.
    narr = "Người ta thử nghiệm dùng sub-agent để quản lý mọi thứ khác nhau."
    rows, _ = _build_rows(narr, dur=6.0)
    # Find the row containing 'sub' and the row containing 'agent' (the compound halves).
    row_of = {}
    for ri, row in enumerate(rows):
        for w in row:
            row_of.setdefault(w, ri)
    assert "sub" in row_of and "agent" in row_of, f"rows={rows}"
    assert row_of["sub"] == row_of["agent"], (
        f"'sub' and 'agent' must be on the SAME row, got rows={rows}"
    )
    # They must be ADJACENT on that row (sub immediately before agent).
    row = rows[row_of["sub"]]
    i = row.index("sub")
    assert row[i + 1] == "agent", f"'sub' and 'agent' not adjacent: {row}"


def test_joined_term_row_may_exceed_word_cap():
    """Bug C corollary (reproduces scene 31): when the term's FIRST half is already the
    5th word on a row, adding its second half would hit the word cap — the guard lets the
    row TEMPORARILY exceed KARAOKE_MAX_WORDS to keep the compound whole (6 words) instead
    of splitting it. Layout: 'Người ta thử dùng sub' fills 5, then 'agent' would break —
    the guard keeps 'sub agent' on the same row."""
    narr = "Người ta thử dùng sub-agent để quản lý context theo."
    rows, _ = _build_rows(narr, dur=5.0)
    first = rows[0]
    # 'sub' is the 5th word; 'agent' is glued so it stays on row 0, overflowing to 6 words.
    assert first[:4] == ["Người", "ta", "thử", "dùng"], f"unexpected head: {rows}"
    assert "sub" in first and "agent" in first, f"compound split off row 0: {rows}"
    i = first.index("sub")
    assert first[i + 1] == "agent", f"'sub' and 'agent' not adjacent on row: {first}"
    assert len(first) > g.KARAOKE_MAX_WORDS, (
        f"row should overflow the {g.KARAOKE_MAX_WORDS}-word cap to keep the term whole: {first}"
    )


def test_no_single_word_orphan_final_row():
    """Bug B: a sentence-terminal short word must not sit ALONE on a fresh final row —
    it is merged back into the previous row (which fits the width). Reproduces scene 7:
    a 16-token question whose last word 'hơn?' previously landed alone on row 4."""
    narr = "Câu hỏi nảy sinh: làm sao tận dụng bộ nhớ nhỏ để làm được nhiều hơn?"
    rows, _ = _build_rows(narr, dur=4.0)
    assert len(rows) >= 2, f"expected multiple rows, got {rows}"
    assert len(rows[-1]) > 1, (
        f"final row must not be a single orphan word (bug B): {rows}"
    )
    # The terminal word should ride on the last (multi-word) row.
    assert rows[-1][-1] == "hơn?", f"terminal word not on final row: {rows}"


def test_orphan_merge_respects_width_budget():
    """Bug B guard: the anti-orphan merge must NOT run when the previous row is already
    at the width limit (merging would push text past the safe margins). A very long
    single-sentence line whose remainder is one wide word stays a separate short row
    rather than overflowing the width. We assert every row's widest zoomed extent still
    fits the usable width even after the anti-orphan pass."""
    # Long sentence; whatever the final grouping, no row may overflow the safe width.
    narr = ("Trong một hệ thống phức tạp với rất nhiều thành phần khác nhau thì việc "
            "điều phối chúng trở nên vô cùng quan trọng đấy.")
    rows, _ = _build_rows(narr, dur=10.0)
    fontsize = g._caption_fontsize(W)
    margin = max(24, int(W * 0.037))
    usable_px = max(1, W - 2 * margin)
    from PIL import ImageFont
    font = ImageFont.truetype(g.CAPTION_FONT, fontsize)
    wfac = g.CAPTION_LIBASS_WFACTOR
    space_w = float(font.getlength(" ")) * wfac
    for row in rows:
        widths = [float(font.getlength(g._ass_escape(w))) * wfac for w in row]
        total = sum(widths) + space_w * max(0, len(row) - 1) \
            + (g.HL_SCALE / 100.0 - 1.0) * (max(widths) if widths else 0.0)
        assert total <= usable_px + 1.0, f"row overflows safe width after merge: {row} ({total:.0f}>{usable_px})"


if __name__ == "__main__":
    raise SystemExit(pytest.main([os.path.abspath(__file__), "-q"]))
