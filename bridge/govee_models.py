"""Capability table of the Govee models and the tier of a light.

Tiers: ``segmented`` models are in the table and carry tested values (segment
count, frame and packet rates, DreamView timing, geometry). Every other model, or a
light forced to ``basic`` in the registry, is a basic light: one color for the whole
lamp through ``colorwc`` plus brightness and power. A model joins the table only after
a live test.
"""

import copy

# Tested values per model. H6076 is the seven-segment floor lamp: its anchor is
# 60 cm above the base, and the segments span 128 cm starting 6 cm above the base.
MODELS = {
    "H6076": {
        "tier": "segmented",
        "segments": 7,
        # DreamView frames per second while Sync streams. With a new color in every
        # frame both lamps answered most status polls up to 30 frames/s; at 35 the
        # left one lost a quarter of them (bridge/evidence/dreamview-status-gaps.json).
        "stream_frames_per_second": 30,
        "max_gradient_packets_per_second": 2,
        # The lamp leaves DreamView after about a minute without frames. Switching the
        # mode on while it is already on blanks the lamp for a moment, so the mode is
        # sent only when the lamp may have left it: at every stream start, after a
        # frame gap close to that timeout, after a verified Off and after a status
        # loss long enough for a power cycle. Short status misses keep it.
        "razer_idle_seconds": 45,
        "razer_lost_seconds": 5,
        "geometry": {"kind": "segmented-floor", "zones": 7, "anchor_offset_cm": 60,
                     "first_cm": 6, "span_cm": 128},
    },
}
TIERS = (None, "basic")
BASIC = {"tier": "basic", "segments": 0}


def capabilities(model, tier=None):
    """Return a copy of the model entry, or the basic entry for other models or ``basic``."""
    if tier not in TIERS:
        raise ValueError("Unknown Govee tier")
    entry = MODELS.get(model) if tier is None and type(model) is str else None
    return copy.deepcopy(entry if entry is not None else BASIC)
