"""Govee model capability table and tiers."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from govee_models import MODELS, capabilities  # noqa: E402

H6076 = {
    "tier": "segmented", "segments": 7, "stream_frames_per_second": 30,
    "max_gradient_packets_per_second": 2, "razer_idle_seconds": 45, "razer_lost_seconds": 5,
    "geometry": {"kind": "segmented-floor", "zones": 7, "anchor_offset_cm": 60,
                 "first_cm": 6, "span_cm": 128},
}


class GoveeModelTests(unittest.TestCase):
    def test_table_holds_the_tested_h6076_values(self):
        self.assertEqual(MODELS, {"H6076": H6076})

    def test_known_model_is_segmented(self):
        self.assertEqual(capabilities("H6076"), H6076)

    def test_unknown_or_missing_model_is_basic(self):
        for model in ("H6008", "", None, "h6076"):
            with self.subTest(model=model):
                self.assertEqual(capabilities(model), {"tier": "basic", "segments": 0})

    def test_basic_override_wins_over_the_table(self):
        self.assertEqual(capabilities("H6076", "basic"), {"tier": "basic", "segments": 0})
        self.assertEqual(capabilities("H6076", None), H6076)

    def test_unknown_tier_is_rejected(self):
        for tier in ("segmented", "Basic", "", 1):
            with self.subTest(tier=tier), self.assertRaises(ValueError):
                capabilities("H6076", tier)

    def test_result_is_a_detached_copy(self):
        capabilities("H6076")["segments"] = 1
        capabilities("H6076")["geometry"]["zones"] = 1
        self.assertEqual(capabilities("H6076"), H6076)
        self.assertEqual(MODELS["H6076"], H6076)
