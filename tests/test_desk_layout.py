import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import LayoutStore, layout_event, positions, validate_layout  # noqa: E402


def layout():
    rows = [
        ("govee-left", 20, 60, 65),
        ("govee-right", 180, 60, 65),
        ("hue-monitor", 100, 110, 100),
        ("play-left", 70, 40, 100),
        ("play-center", 100, 40, 50),
        ("play-right", 130, 40, 100),
    ]
    return {
        "version": 3,
        "gridStepCm": 5,
        "room": "Office",
        "desk": {"width": 160, "height": 85},
        "monitorInches": 32,
        "devices": [
            {
                "id": key,
                "scale": scale,
                "positionCm": {"x": x, "height": h},
                "orientation": "horizontal",
            }
            for key, x, h, scale in rows
        ],
    }


def named(**names):
    data = layout()
    data["version"] = 5
    for row in data["devices"]:
        if row["id"] in names:
            row["name"] = names[row["id"]]
    return data


class LayoutTests(unittest.TestCase):
    def test_role_rows_accept_an_optional_stripped_name(self):
        result = validate_layout(named(**{"govee-left": "  Desk left  ", "play-center": "C" * 32,
                                          "govee-right": "R" * 35}))
        rows = {row["id"]: row for row in result["devices"]}
        self.assertEqual(rows["govee-left"]["name"], "Desk left")
        self.assertEqual(rows["play-center"]["name"], "C" * 32)
        self.assertEqual(rows["govee-right"]["name"], "R" * 35)
        # Missing or null means no owner name, and nothing is stored.
        self.assertNotIn("name", rows["hue-monitor"])
        result = validate_layout(named(**{"hue-monitor": None}))
        self.assertTrue(all("name" not in row for row in result["devices"]))
        self.assertEqual(result["version"], 5)

    def test_role_names_are_bounded_printable_text(self):
        for role, name in (("govee-left", ""), ("govee-left", "   "), ("govee-left", "x" * 36),
                           ("hue-monitor", "x" * 33), ("play-left", "x" * 33),
                           ("govee-left", "tab\there"), ("govee-left", "line\nbreak"),
                           ("govee-left", "bell\x07"), ("govee-left", "nul\x00"),
                           ("govee-left", "zero\u200bwidth"), ("govee-left", 5),
                           ("govee-left", ["x"])):
            with self.assertRaises(ValueError, msg=repr((role, name))):
                validate_layout(named(**{role: name}))
        # A padded name is measured after stripping.
        validate_layout(named(**{"hue-monitor": " " + "x" * 32 + " "}))

    def test_store_names_maps_adapter_roles_to_owner_names(self):
        with TemporaryDirectory(prefix="tmp_layout_names_") as folder:
            store = LayoutStore(Path(folder) / "layout.json")
            self.assertEqual(store.names(), {})
            store.save(layout())
            self.assertEqual(store.names(), {})
            store.save(named(**{"govee-left": "Desk left", "hue-monitor": "Monitor bar",
                                "play-right": "Right Play"}))
            self.assertEqual(store.names(), {"left": "Desk left", "strip": "Monitor bar",
                                             "hue_right": "Right Play"})
            self.assertNotEqual(store.revision(), LayoutStore.digest(validate_layout(layout())))
            store.save(named(**{"govee-left": "Floor lamp"}))
            self.assertEqual(store.names(), {"left": "Floor lamp"})

    def test_names_of_unbound_rows_keep_their_rules(self):
        data = layout()
        data["devices"].append({"id": "virtual-abc", "scale": 50, "shape": "bar",
                                "positionCm": {"x": 50, "height": 40}, "name": "x" * 35})
        self.assertEqual(validate_layout(data)["devices"][-1]["name"], "x" * 35)
        data["devices"][-1]["name"] = "x" * 36
        with self.assertRaises(ValueError):
            validate_layout(data)

    def test_layout_event_retains_complete_entertainment_resource(self):
        resource = {
            "id": "desk-area", "type": "entertainment_configuration",
            "status": "active", "configuration_type": "monitor",
            "metadata": {"name": "Desk"},
            "stream_proxy": {"mode": "auto", "node": {"rid": "proxy"}},
            "channels": [{"channel_id": 0}],
            "locations": {"service_locations": []},
            "light_services": [{"rid": "lamp", "rtype": "light"}],
        }
        event = layout_event(resource)
        self.assertEqual(event["type"], "update")
        self.assertEqual(event["data"], [resource])
        resource["metadata"]["name"] = "Later edit"
        self.assertEqual(event["data"][0]["metadata"]["name"], "Desk")

    def test_response_revision_is_computed_from_the_returned_snapshot(self):
        with TemporaryDirectory(prefix="tmp_layout_revision_") as folder:
            store = LayoutStore(Path(folder) / "layout.json")
            expected = store.save(layout())
            store.revision = lambda: (_ for _ in ()).throw(
                AssertionError("second independent read")
            )
            self.assertEqual(store.response(), expected)

    def test_monitor_strip_and_signe_anchors_keep_channel_identity(self):
        points = positions(validate_layout(layout()))
        self.assertEqual(len(points), 18)
        self.assertEqual(points[2], (-0.4, 0.8, -0.4))
        self.assertEqual(points[6], (0, 0.8, 0.4))
        self.assertEqual(points[10], (0.4, 0.8, -0.4))
        self.assertLess(points[0][0], 0)
        self.assertGreater(points[1][0], 0)
        self.assertLessEqual(points[0][2], points[14][2])
        self.assertLess(points[14][2], points[15][2])

    def test_moving_monitor_repositions_other_lights_in_screen_space(self):
        data = layout()
        before = positions(validate_layout(data))
        data["devices"][2]["positionCm"] = {"x": 110, "height": 120}
        after = positions(validate_layout(data))
        self.assertEqual(after[2:11], before[2:11])
        self.assertLess(after[1][0], before[1][0])
        self.assertLess(after[15][2], before[15][2])

    def test_play_rotation_does_not_create_more_channels_or_move_its_center(self):
        data = layout()
        before = positions(validate_layout(data))
        data["devices"][3]["orientation"] = "vertical"
        self.assertEqual(positions(validate_layout(data)), before)

    def test_clipped_gradient_keeps_three_distinct_linear_anchors(self):
        data = layout()
        data["devices"][2]["positionCm"]["height"] = 120
        points = positions(validate_layout(data))
        z = [points[i][2] for i in (0, 14, 15)]
        self.assertLess(z[0], z[1])
        self.assertLess(z[1], z[2])
        self.assertAlmostEqual(z[1], (z[0] + z[2]) / 2, places=5)
        data["devices"][2]["positionCm"]["height"] = 250
        points = positions(validate_layout(data))
        z = [points[i][2] for i in (0, 14, 15)]
        self.assertLess(z[0], z[1])
        self.assertLess(z[1], z[2])

    def test_virtual_drawings_do_not_authorize_any_physical_output(self):
        data = layout()
        data["devices"].append(
            {
                "id": "virtual-extra",
                "shape": "floor",
                "name": "Extra",
                "scale": 80,
                "positionCm": {"x": 200, "height": 60},
            }
        )
        self.assertEqual(positions(validate_layout(data)), positions(validate_layout(layout())))
        data["devices"][-1]["id"] = "third-govee"
        with self.assertRaises(ValueError):
            validate_layout(data)

    def test_rejects_duplicates_nonfinite_values_and_control_fields(self):
        for mutate in [
            lambda d: d["devices"].append(copy.deepcopy(d["devices"][0])),
            lambda d: d["devices"][0].update(ip="192.168.10.123"),
            lambda d: d["devices"][0].update(scale=True),
            lambda d: d["devices"][0]["positionCm"].update(height=float("nan")),
            lambda d: d["devices"].pop(),
        ]:
            data = layout()
            mutate(data)
            with self.assertRaises(ValueError):
                validate_layout(data)

    def test_older_versions_load_and_always_return_version_five(self):
        for version in (3, 4, 5):
            data = layout()
            data["version"] = version
            with self.subTest(version=version):
                self.assertEqual(validate_layout(data)["version"], 5)
        for version in (2, 6, "4", 4.0, True, None):
            data = layout()
            data["version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_layout(data)

    def test_version_three_file_is_upgraded_on_the_next_save(self):
        with TemporaryDirectory(prefix="tmp_desk_layout_v3_") as folder:
            path = Path(folder) / "layout.json"
            path.write_text(json.dumps(layout()))
            store = LayoutStore(path)
            self.assertEqual(store.snapshot()["version"], 5)
            store.save(store.snapshot())
            self.assertEqual(json.loads(path.read_text())["version"], 5)

    def test_atomic_persistence_and_invalid_file_keep_last_good_configuration(self):
        with TemporaryDirectory(prefix="tmp_desk_layout_") as folder:
            path = Path(folder) / "layout.json"
            writer = LayoutStore(path)
            reader = LayoutStore(path)
            self.assertIsNone(reader.snapshot())
            writer.save(layout())
            self.assertEqual(reader.scale("left", 1), 0.65)
            self.assertEqual(reader.scale("hue_center", 1), 0.5)
            previous = reader.snapshot()
            path.write_text("{broken")
            self.assertEqual(reader.snapshot(), previous)
