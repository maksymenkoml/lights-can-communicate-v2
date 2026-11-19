import copy
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LayoutStore, validate_layout  # noqa: E402
from govee_calibration import GoveeCalibration, curve  # noqa: E402
from govee_segments import razer_colors, razer_data, razer_mode, segment_data  # noqa: E402
from test_two_configurations import row, v5  # noqa: E402

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"
CALIBRATED = {"side": "left", "device": LEFT}
PLAIN = {"side": "right", "device": RIGHT}
# The owner's 2026-11-19 match of the left lamp to the right one: weaker red and
# blue (gains about 0.9, 1, 0.9) and a brighter low-level curve (exponent 0.482).
LEFT_MATCH = {"warmth": 0, "tint": -33, "lowLight": 74}


class FakeStore:
    def __init__(self, **sides):
        self.sides = sides

    def calibration(self, side):
        return dict(self.sides.get(side, {"warmth": 0, "tint": 0, "lowLight": 0}))


def ptreal_colors(data):
    import base64
    return [list(base64.b64decode(item)[4:7]) for item in data["command"]]


class GoveeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.store = FakeStore(left=LEFT_MATCH)
        self.calibration = GoveeCalibration([CALIBRATED, PLAIN], self.store)
        self.send = self.calibration.wrap(
            lambda device, command, data: self.sent.append((device["side"], command, data)) or True)

    def test_measured_matches_are_reproduced(self):
        self.send(CALIBRATED, "brightness", {"value": 35})
        self.send(CALIBRATED, "brightness", {"value": 10})
        self.send(CALIBRATED, "brightness", {"value": 100})
        self.assertEqual([data["value"] for _, _, data in self.sent], [60, 33, 100])
        self.sent.clear()
        self.send(CALIBRATED, "razer", razer_data([[255, 235, 207], [255, 255, 255]]))
        self.assertEqual(razer_colors(self.sent[0][2]), [[249, 255, 202], [230, 255, 230]])

    def test_a_lamp_without_calibration_gets_the_same_data(self):
        for command, data in (("brightness", {"value": 35}),
                              ("razer", razer_data([[255, 235, 207]])),
                              ("ptReal", segment_data([[10, 20, 30]] * 7)),
                              ("colorwc", {"color": {"r": 1, "g": 2, "b": 3},
                                           "colorTemInKelvin": 0})):
            self.send(PLAIN, command, data)
            self.assertIs(self.sent[-1][2], data)

    def test_every_colour_command_is_calibrated_and_the_mode_and_power_are_not(self):
        self.send(CALIBRATED, "ptReal", segment_data([[255, 235, 207]] * 7))
        self.assertEqual(ptreal_colors(self.sent[-1][2]), [[249, 255, 202]] * 7)
        self.send(CALIBRATED, "colorwc", {"color": {"r": 255, "g": 235, "b": 207},
                                          "colorTemInKelvin": 0})
        self.assertEqual(self.sent[-1][2]["color"], {"r": 249, "g": 255, "b": 202})
        kelvin = {"color": {"r": 0, "g": 0, "b": 0}, "colorTemInKelvin": 2700}
        mode, turn = razer_mode(True), {"value": 1}
        self.send(CALIBRATED, "colorwc", kelvin)
        self.send(CALIBRATED, "razer", mode)
        self.send(CALIBRATED, "turn", turn)
        self.assertEqual([item[2] for item in self.sent[-3:]], [kelvin, mode, turn])
        self.send(CALIBRATED, "razer", razer_data([[0, 0, 0]]))
        self.assertEqual(razer_colors(self.sent[-1][2]), [[0, 0, 0]])

    def test_statuses_read_back_in_the_values_that_were_asked_for(self):
        for value in range(0, 101):
            self.sent.clear()
            self.send(CALIBRATED, "brightness", {"value": value})
            sent = self.sent[0][2]["value"]
            self.assertTrue(value == 0 or 1 <= sent <= 100)
            status = {"onOff": 1, "brightness": sent, "identity_verified": True,
                      "color": {"r": 249, "g": 255, "b": 202}, "colorTemInKelvin": 0}
            decoded = self.calibration.decode({LEFT: (status, 5.0)})
            self.assertEqual(decoded[LEFT][0]["brightness"], value)
            self.assertEqual(decoded[LEFT][1], 5.0)
            self.assertEqual(status["brightness"], sent, "the transport status is not changed")
        self.assertEqual(decoded[LEFT][0]["color"], {"r": 255, "g": 235, "b": 207})
        self.assertTrue(decoded[LEFT][0]["identity_verified"])
        plain = ({"onOff": 1, "brightness": 35}, 1.0)
        self.assertIs(self.calibration.decode({RIGHT: plain})[RIGHT], plain)
        missing = (None, 0)
        self.assertIs(self.calibration.decode({LEFT: missing})[LEFT], missing)

    def test_a_level_set_elsewhere_reads_back_through_the_curve(self):
        # 99 and 100 both reach the lamp as 100; the value this adapter sent wins.
        self.send(CALIBRATED, "brightness", {"value": 99})
        self.assertEqual(self.sent[-1][2]["value"], 100)
        def status(value):
            return {LEFT: ({"onOff": 1, "brightness": value}, 1.0)}

        self.assertEqual(self.calibration.decode(status(100))[LEFT][0]["brightness"], 99)
        # The Govee app set 80: 100 * 0.8 ** (1 / 0.482) is 63.
        self.assertEqual(self.calibration.decode(status(80))[LEFT][0]["brightness"], 63)
        self.assertEqual(self.calibration.decode(status(1))[LEFT][0]["brightness"], 1)

    def test_a_profile_calibration_refuses_to_start(self):
        # The layout is the only source, so a lamp is never calibrated twice.
        for field, value in (("color_gains", [0.9, 1, 0.9]), ("brightness_exponent", 0.5)):
            with self.subTest(field=field), self.assertRaises(ValueError):
                GoveeCalibration([{**PLAIN, field: value}])

    def test_the_owner_match_to_the_hue_lamps_maps_to_measured_gains(self):
        self.assertEqual(curve({"warmth": 0, "tint": 0, "lowLight": 0}), (None, None))
        gains, exponent = curve({"warmth": 83, "tint": 0, "lowLight": 0})
        self.assertIsNone(exponent)
        self.assertEqual([round(g, 2) for g in gains], [1, 0.85, 0.5])
        gains, exponent = curve({"warmth": 83, "tint": -33, "lowLight": 74})
        self.assertEqual([round(g / gains[0] * 0.9, 2) for g in gains], [0.9, 0.85, 0.45])
        self.assertAlmostEqual(exponent, 0.482)
        cooler, _ = curve({"warmth": -50})
        pinker, _ = curve({"tint": 50})
        self.assertLess(cooler[0], cooler[2])
        self.assertLess(pinker[1], pinker[0])

    def test_a_session_override_wins_until_it_ends_and_the_layout_is_read_live(self):
        self.calibration.override("right", {"warmth": 83})
        self.send(PLAIN, "razer", razer_data([[255, 235, 207]]))
        self.assertEqual(razer_colors(self.sent[-1][2]), [[255, 200, 104]])
        self.calibration.override("right", None)
        self.send(PLAIN, "razer", razer_data([[255, 235, 207]]))
        self.assertEqual(razer_colors(self.sent[-1][2]), [[255, 235, 207]])
        self.store.sides["right"] = {"warmth": 83, "tint": 0, "lowLight": 0}
        self.send(PLAIN, "razer", razer_data([[255, 235, 207]]))
        self.assertEqual(razer_colors(self.sent[-1][2]), [[255, 200, 104]])


class LayoutCalibrationTests(unittest.TestCase):
    def test_version_five_loads_as_six_without_calibration(self):
        result = validate_layout(v5())
        self.assertEqual(result["version"], 6)
        self.assertTrue(all("calibration" not in item for item in result["devices"]))

    def test_govee_rows_store_their_calibration_and_zero_is_not_stored(self):
        data = v5(version=6)
        row(data, "govee-left")["calibration"] = {"warmth": 83, "tint": -33, "lowLight": 73}
        row(data, "govee-right")["calibration"] = {"warmth": 0, "tint": 0, "lowLight": 0}
        result = validate_layout(data)
        self.assertEqual(row(result, "govee-left")["calibration"],
                         {"warmth": 83, "tint": -33, "lowLight": 73})
        self.assertNotIn("calibration", row(result, "govee-right"))
        self.assertEqual(validate_layout(copy.deepcopy(result)), result)
        partial = v5(version=6)
        row(partial, "govee-left")["calibration"] = {"warmth": 20}
        self.assertEqual(row(validate_layout(partial), "govee-left")["calibration"],
                         {"warmth": 20, "tint": 0, "lowLight": 0})

    def test_invalid_calibration_is_refused(self):
        for key, value in (("play-left", {"warmth": 10}), ("govee-left", {"warmth": 101}),
                           ("govee-left", {"tint": -101}), ("govee-left", {"lowLight": -1}),
                           ("govee-left", {"warmth": 1.5}), ("govee-left", {"warmth": True}),
                           ("govee-left", {"hue": 1}), ("govee-left", [83, 0, 0])):
            data = v5(version=6)
            row(data, key)["calibration"] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                validate_layout(data)

    def test_the_store_reads_a_side_calibration(self):
        with TemporaryDirectory(prefix="tmp_calibration_") as folder:
            store = LayoutStore(Path(folder) / "desk-layout.json")
            self.assertEqual(store.calibration("left"), {"warmth": 0, "tint": 0, "lowLight": 0})
            data = v5(version=6)
            row(data, "govee-left")["calibration"] = {"warmth": 83, "tint": -33, "lowLight": 73}
            store.save(data)
            self.assertEqual(store.calibration("left"), {"warmth": 83, "tint": -33, "lowLight": 73})
            self.assertEqual(store.calibration("right"), {"warmth": 0, "tint": 0, "lowLight": 0})


if __name__ == "__main__":
    unittest.main()
