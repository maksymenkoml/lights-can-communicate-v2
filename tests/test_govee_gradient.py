import ast
import importlib.util
import os
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

FOLDER = Path(__file__).resolve().parents[1] / "bridge"
sys.path.insert(0, str(FOLDER))
from gradient_profile import configure_signe, validate_gradient  # noqa: E402


def gradient_stream():
    spec = importlib.util.spec_from_file_location(
        "gradient_stream_test", FOLDER / "entertainment_stream.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"PROBE_GOVEE_GRADIENT": "true"}):
        spec.loader.exec_module(module)
    return module


class GradientProfileTests(unittest.TestCase):
    def test_gradient_payload_validation_precedes_persistent_state_changes(self):
        point = {"color": {"xy": {"x": 0.3, "y": 0.3}}}
        # The Hue app sends up to five points to every gradient light, also
        # to one that advertises three; the adapter spreads them over the zones.
        for count in (1, 3, 5):
            validate_gradient({"points": [point] * count})
        for value in (
            None,
            {},
            {"points": None},
            {"points": []},
            {"points": [None]},
            {"points": [point] * 6},
            {"points": [{"color": {"xy": {"x": True, "y": 0.3}}}]},
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_gradient(value)

    def test_enabling_gradient_preserves_current_white_mode_on_brightness_change(self):
        from live_adapter import normalize_state

        lamp = SimpleNamespace(
            modelid="LCT015",
            state={"on": True, "bri": 100, "colormode": "ct", "ct": 400, "xy": [0.15, 0.06]},
            config={"archetype": "sultanbulb"},
            protocol_cfg={},
            function="mixed",
        )
        configure_signe(lamp, True)
        payload = {**lamp.state, "_color_kind": lamp.protocol_cfg["proxy_color_kind"]}
        self.assertEqual(normalize_state(payload, 0.65, 7)["kelvin"], 2500)

    def test_reenable_after_legacy_color_change_does_not_restore_stale_gradient(self):
        from live_adapter import normalize_state

        lamp = SimpleNamespace(
            modelid="LCT015",
            state={"on": True, "bri": 100, "colormode": "xy", "xy": [0.15, 0.06]},
            config={"archetype": "sultanbulb"},
            protocol_cfg={},
            function="mixed",
        )
        configure_signe(lamp, True)
        configure_signe(lamp, False)
        lamp.state["xy"] = [0.7, 0.299]
        configure_signe(lamp, True)
        payload = {**lamp.state, "_color_kind": lamp.protocol_cfg["proxy_color_kind"]}
        self.assertEqual(normalize_state(payload, 0.65, 7)["rgb"], (255, 0, 0))

    def test_signe_profile_preserves_resource_identity_and_power(self):
        lamp = SimpleNamespace(
            id_v1="1",
            id_v2="stable-id",
            name="Govee Left",
            modelid="LCT015",
            state={"on": False, "bri": 100, "xy": [0.3, 0.3]},
            config={"archetype": "classicbulb"},
            protocol_cfg={},
            function="mixed",
        )
        configure_signe(lamp, True)
        self.assertEqual((lamp.id_v1, lamp.id_v2, lamp.name), ("1", "stable-id", "Govee Left"))
        self.assertFalse(lamp.state["on"])
        self.assertEqual(lamp.state["bri"], 100)
        self.assertEqual(lamp.modelid, "915005987201")
        self.assertEqual(lamp.config["archetype"], "huesigne")
        self.assertEqual(lamp.protocol_cfg["points_capable"], 3)
        self.assertEqual(len(lamp.state["gradient"]["points"]), 3)
        configure_signe(lamp, False)
        self.assertEqual(lamp.modelid, "LCT015")
        self.assertFalse(lamp.state["on"])


class GradientStreamTests(unittest.TestCase):
    def test_bridge_area_lists_channels_in_id_order_with_correct_members(self):
        stream = gradient_stream()
        tree = ast.parse((FOLDER / "bootstrap.py").read_text())
        function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "desk_area_api"
        )
        namespace = {
            "original_area_api": lambda area: {},
            "GROUP_ID": stream.GROUP_ID,
            "SLOT_CHANNELS": stream.SLOT_CHANNELS,
            "CAPTURE_POSITIONS": stream.CAPTURE_POSITIONS,
            "uuid": uuid,
            "layout_store": SimpleNamespace(snapshot=lambda: None),
            "GRADIENT_MODE": True,
            "state": FOLDER / "unused-test-state",
            "config": {
                "lights": {
                    slot: SimpleNamespace(id_v2="light-" + slot) for slot in stream.SLOT_CHANNELS
                }
            },
        }
        exec(
            compile(ast.Module(body=[function], type_ignores=[]), "bootstrap.py", "exec"), namespace
        )
        area = namespace["desk_area_api"](SimpleNamespace(id_v2=stream.GROUP_ID))
        self.assertEqual([channel["channel_id"] for channel in area["channels"]], list(range(18)))
        self.assertEqual(area["channels"][14]["members"][0]["index"], 1)
        self.assertEqual(area["channels"][15]["members"][0]["index"], 2)

    def test_eighteen_channels_keep_native_ids_and_three_anchors_per_govee(self):
        stream = gradient_stream()
        self.assertEqual(stream.CHANNELS, set(range(18)))
        self.assertEqual(stream.SLOT_CHANNELS["1"], [0, 14, 15])
        self.assertEqual(stream.SLOT_CHANNELS["2"], [1, 16, 17])
        self.assertEqual(stream.SLOT_CHANNELS["3"], list(range(2, 11)))
        for side in ("1", "2"):
            anchors = [stream.POSITIONS[i] for i in stream.SLOT_CHANNELS[side]]
            self.assertEqual(len({(p[0], p[1]) for p in anchors}), 1)
            self.assertEqual([p[2] for p in anchors], [-0.8, 0, 0.8])
        frame = {i: (1000 + i, 2000 + i, 3000 + i) for i in range(18)}
        native = stream.native_colors(frame)
        self.assertEqual(native[0], (506, 1006, 1506))
        self.assertEqual(native[3], frame[2])
        self.assertEqual(native[11], frame[10])

    def test_gradient_v2_fragmentation_and_lease_reject_incomplete_schema(self):
        stream = gradient_stream()
        colors = {i: (65535, i, 0) for i in range(18)}
        packet = stream.encode_frame(stream.AREA_ID, colors, 1)
        self.assertEqual(stream.decode_frame(packet), colors)
        buffer = stream.FrameBuffer()
        self.assertEqual(buffer.feed(packet[:71]), [])
        self.assertEqual(buffer.feed(packet[71:] + packet), [packet, packet])
        with self.assertRaises(ValueError):
            stream.decode_frame(
                stream.encode_frame(stream.AREA_ID, dict(list(colors.items())[:14]), 1)
            )
        lease = stream.VideoLease(wall_clock=lambda: 1000)
        envelope = {
            "action": "frame",
            "session": "test",
            "sequence": 1,
            "issued_at": 1000,
            "colors": [[k, *v] for k, v in colors.items()],
        }
        self.assertTrue(lease.submit(envelope))
        envelope["colors"] = envelope["colors"][:14]
        with self.assertRaises(ValueError):
            lease.submit(envelope)
