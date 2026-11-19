import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from govee_calibration import GoveeCalibration  # noqa: E402
from govee_segments import razer_colors, razer_data, razer_mode, segment_data  # noqa: E402

LEFT = "AA:AA:AA:AA:AA:AA:AA:AA"
RIGHT = "BB:BB:BB:BB:BB:BB:BB:BB"
# The owner's 2026-11-19 match: the left lamp needs weaker red and blue and a
# brighter curve to look like the right one.
CALIBRATED = {"side": "left", "device": LEFT, "color_gains": [0.9, 1, 0.9],
              "brightness_exponent": 0.487}
PLAIN = {"side": "right", "device": RIGHT}


def ptreal_colors(data):
    import base64
    return [list(base64.b64decode(item)[4:7]) for item in data["command"]]


class GoveeCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.calibration = GoveeCalibration([CALIBRATED, PLAIN])
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
        # The Govee app set 80: 100 * 0.8 ** (1 / 0.487) is 63.
        self.assertEqual(self.calibration.decode(status(80))[LEFT][0]["brightness"], 63)
        self.assertEqual(self.calibration.decode(status(1))[LEFT][0]["brightness"], 1)

    def test_invalid_profiles_refuse_to_start(self):
        for field, value in (("color_gains", [0.9, 1]), ("color_gains", [0, 1, 1]),
                             ("color_gains", [1.2, 1, 1]), ("color_gains", "0.9"),
                             ("color_gains", [True, 1, 1]), ("brightness_exponent", 0),
                             ("brightness_exponent", 1.5), ("brightness_exponent", "0.5"),
                             ("brightness_exponent", float("nan"))):
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                GoveeCalibration([{**PLAIN, field: value}])


if __name__ == "__main__":
    unittest.main()
