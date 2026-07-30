"""Unit tests for _blur_cover_filtergraph (translate_full caption-cover).

Regression guard for the filter-graph shape, NOT for pixel output. The bug this locks
down: the builder used to emit one `split -> crop -> gblur -> overlay` stage PER cover
interval, chained. Each `overlay` is a framesync filter reconciling two branches, and
~30 of them deep hits an ffmpeg scheduling cliff — the graph could not emit even the
FIRST frame (measured: >120s stuck at frame=1 on a 33-interval 1080x1920 body) and hung
four consecutive production renders. The rewrite composites through ONE mask
(drawbox per interval -> alphamerge -> a single overlay), so the number of frame-syncing
filters is constant in the interval count.

Pure string tests: no ffmpeg, no files, no GPU.

Run:  cd Dashboard/api && .venv/Scripts/python -m pytest test/test_blur_cover_filtergraph.py -q
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate as G  # noqa: E402

W, H = 1080, 1920


def _boxes(n, base_w=900.0, box_h=120.0):
    """n contiguous, centered, karaoke-band cover intervals (the real translate_full shape)."""
    out = []
    for i in range(n):
        w = base_w - (i % 5) * 40.0
        out.append({"box": [W / 2.0 - w / 2.0, 1376.0, w, box_h],
                    "start": i * 2.0, "end": i * 2.0 + 1.9})
    return out


def _fc(ivs):
    return G._blur_cover_filtergraph(ivs, W, H, "[0:v]", "[vc]")


def test_no_intervals_returns_none():
    assert _fc([]) is None
    assert _fc(None) is None


def test_degenerate_boxes_only_returns_none():
    # Under _clamp_box's 8px floor -> nothing to cover.
    assert _fc([{"box": [10, 10, 4, 4], "start": 0.0, "end": 1.0}]) is None


def test_overlay_count_does_not_grow_with_intervals():
    """THE regression: interval count must not multiply frame-syncing filters."""
    for n in (2, 5, 12, 33, TF_MAX := G.TF_COVER_MAX_INTERVALS):
        fc = _fc(_boxes(n))
        assert fc is not None, n
        assert fc.count("overlay=") == 1, f"n={n} emitted {fc.count('overlay=')} overlays"
        assert fc.count("alphamerge") == 1, n
        assert fc.count("gblur=") == 1, f"n={n} must blur ONCE, not per interval"
    assert TF_MAX  # silence the walrus-unused lint


def test_one_drawbox_per_interval_plus_canvas():
    for n in (2, 7, 33):
        fc = _fc(_boxes(n))
        # n white interval boxes + 1 black canvas fill
        assert fc.count("drawbox=") == n + 1, n


def test_time_gate_is_preserved_per_interval():
    ivs = _boxes(3)
    fc = _fc(ivs)
    for iv in ivs:
        assert f"enable='between(t,{iv['start']:.3f},{iv['end']:.3f})'" in fc


def test_single_interval_uses_plain_overlay_no_mask():
    fc = _fc(_boxes(1))
    assert fc is not None
    assert "alphamerge" not in fc and fc.count("overlay=") == 1


def test_above_cap_collapses_to_one_untimed_union_blur():
    fc = _fc(_boxes(G.TF_COVER_MAX_INTERVALS + 1))
    assert fc is not None
    assert "alphamerge" not in fc          # collapsed path, no mask
    assert "enable=" not in fc             # union blur is persistent, not time-gated
    assert fc.count("overlay=") == 1


def test_returns_fragment_ending_in_out_label():
    """Caller contract: the fragment must terminate on the requested label."""
    for n in (1, 4, 33, G.TF_COVER_MAX_INTERVALS + 1):
        fc = _fc(_boxes(n))
        assert fc.endswith("[vc]"), n
        assert fc.startswith("[0:v]"), n


def test_format_pins_present():
    """Both pins are load-bearing (see the function's comment).

    Without the mask-branch yuv420p pin, libavfilter back-propagates `gray` through the
    split and the WHOLE video renders black-and-white; without overlay's yuv420 pin the
    full-size main frame is converted to yuva420p every frame.
    """
    fc = _fc(_boxes(6))
    assert "format=yuv420p,crop=" in fc
    assert "overlay=" in fc and ":format=yuv420[vc]" in fc


def test_mask_boxes_are_relative_to_the_union_crop():
    """drawbox coords are union-relative; every box must land inside the cropped band."""
    import re
    fc = _fc(_boxes(9))
    crop = re.search(r"format=yuv420p,crop=(\d+):(\d+):(\d+):(\d+)", fc)
    uw, uh = int(crop.group(1)), int(crop.group(2))
    for m in re.finditer(r"drawbox=x=(\d+):y=(\d+):w=(\d+):h=(\d+)", fc):
        x, y, w, h = (int(g) for g in m.groups())
        assert 0 <= x and x + w <= uw, m.group(0)
        assert 0 <= y and y + h <= uh, m.group(0)


def test_sigma_tracks_the_tallest_box():
    """One shared blur pass -> sigma comes from max(box height) * FACTOR (floored)."""
    ivs = _boxes(4, box_h=120.0)
    ivs[2]["box"][3] = 200.0                       # one much taller box
    fc = _fc(ivs)
    tallest = G._clamp_box(ivs[2]["box"], W, H, margin=1)[3]
    expect = max(G.TF_COVER_BLUR_SIGMA_MIN, tallest * G.TF_COVER_BLUR_SIGMA_FACTOR)
    assert f"gblur=sigma={expect:.1f}:" in fc
