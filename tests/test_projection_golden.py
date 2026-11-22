"""Sync and scene projection outputs stay byte-identical to the pinned fixture."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import golden_projection as golden  # noqa: E402

PINNED = json.loads(golden.FIXTURE.read_text())["cases"]


class ProjectionGoldenTests(unittest.TestCase):
    def test_fixture_covers_the_generator_cases(self):
        self.assertEqual(sorted(PINNED), sorted(case["name"] for case in golden.cases()))
        self.assertGreaterEqual(len(PINNED), 12)

    def test_every_output_matches_the_pinned_fixture(self):
        for name, pinned in PINNED.items():
            case = {"name": name, "layout": pinned["layout"], "frames": pinned["frames"]}
            result = golden.compute(case)
            for key in ("native", "govee", "scene_fade", "positions", "zone"):
                with self.subTest(case=name, output=key):
                    self.assertEqual(result[key], pinned[key])

    def test_generator_inputs_match_the_fixture_inputs(self):
        for case in golden.cases():
            with self.subTest(case=case["name"]):
                pinned = PINNED[case["name"]]
                self.assertEqual(golden.jsonable(case["layout"]), pinned["layout"])
                self.assertEqual(case["frames"], pinned["frames"])


if __name__ == "__main__":
    unittest.main()
