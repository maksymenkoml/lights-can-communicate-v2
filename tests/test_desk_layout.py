import copy
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from desk_layout import (  # noqa: E402
    LAYOUT_VERSION,
    MIGRATED_DESK,
    LayoutStore,
    layout_event,
    positions,
    screen_size,
    screen_support,
    validate_layout,
)


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


def version_seven():
    data = layout()
    data["version"] = 7
    del data["monitorInches"]
    data["screen"] = {"positionCm": {"x": 100, "height": 110}, "inches": 32}
    return data


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
        self.assertEqual(result["version"], LAYOUT_VERSION)

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
            self.assertEqual(store.names(), {"govee-left": "Desk left",
                                             "hue-monitor": "Monitor bar",
                                             "play-right": "Right Play"})
            self.assertNotEqual(store.revision(), LayoutStore.digest(validate_layout(layout())))
            store.save(named(**{"govee-left": "Floor lamp"}))
            self.assertEqual(store.names(), {"govee-left": "Floor lamp"})

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

    def test_v6_migrates_screen_from_monitor_row(self):
        data = layout()
        data["version"] = 6
        data["monitorInches"] = 43
        data["devices"][2]["positionCm"] = {"x": 60, "height": 130}
        result = validate_layout(data)
        self.assertEqual(result["screen"], {"kind": "monitor",
                                            "positionCm": {"x": 60, "height": 130},
                                            "inches": 43, "on": MIGRATED_DESK})
        self.assertNotIn("monitorInches", result)
        monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
        self.assertEqual(monitor["positionCm"], {"x": 60, "height": 130})

    def test_v6_screen_keeps_the_height_raised_by_the_minimum(self):
        data = layout()
        data["version"] = 6
        data["monitorInches"] = 55
        data["devices"][2]["positionCm"]["height"] = 20
        result = validate_layout(data)
        self.assertEqual(result["screen"]["positionCm"], {"x": 100, "height": 40})
        monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
        self.assertEqual(monitor["positionCm"]["height"], 40)

    def test_v7_requires_screen_and_rejects_monitor_inches(self):
        data = version_seven()
        broken = copy.deepcopy(data)
        del broken["screen"]
        with self.assertRaises(ValueError):
            validate_layout(broken)
        broken = copy.deepcopy(data)
        broken["monitorInches"] = 32
        with self.assertRaises(ValueError):
            validate_layout(broken)
        legacy = layout()
        legacy["screen"] = copy.deepcopy(data["screen"])
        with self.assertRaises(ValueError):
            validate_layout(legacy)
        for key, extra in (("desk", {"width": 160, "height": 85}), ("monitorInches", 32)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_layout({**validate_layout(data), key: extra})

    def test_older_versions_migrate_to_furniture_and_a_monitor(self):
        for data in (layout(), version_seven()):
            with self.subTest(version=data["version"]):
                data["desk"] = {"width": 140, "height": 75}
                result = validate_layout(data)
                self.assertEqual(result["furniture"], [{"id": MIGRATED_DESK, "kind": "desk",
                                                        "x": 100, "width": 140, "height": 75}])
                self.assertEqual((result["screen"]["kind"], result["screen"]["on"]),
                                 ("monitor", MIGRATED_DESK))
                self.assertNotIn("desk", result)
                self.assertNotIn("monitorInches", result)
                self.assertEqual(screen_support(result)["id"], MIGRATED_DESK)
                self.assertEqual(validate_layout(result), result, "version eight round-trips")

    def test_furniture_ranges_and_ids(self):
        base = validate_layout(layout())

        def with_pieces(*pieces):
            return {**base, "furniture": list(pieces)}

        def piece(identifier="piece-0000000a", kind="cabinet", width=45, height=55, x=230):
            return {"id": identifier, "kind": kind, "x": x, "width": width, "height": height}

        accepted = validate_layout(with_pieces(base["furniture"][0], piece(width=20, height=150),
                                               piece("piece-0000000b", width=300, height=20)))
        self.assertEqual(len(accepted["furniture"]), 3)
        for bad in (piece(width=19), piece(height=151), piece(kind="desk", width=79),
                    piece(kind="bed"), piece(identifier="desk-1"), piece(x=231),
                    {**piece(), "depth": 40}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_layout(with_pieces(base["furniture"][0], bad))
        with self.assertRaises(ValueError):
            validate_layout(with_pieces(base["furniture"][0], piece(MIGRATED_DESK)))
        too_many = [piece("piece-%08x" % index) for index in range(13)]
        with self.assertRaises(ValueError):
            validate_layout({**base, "furniture": too_many, "screen": None})

    def test_the_screen_is_optional_and_stands_on_a_piece_of_the_room(self):
        base = validate_layout(layout())
        bare = {key: value for key, value in base.items() if key != "screen"}
        result = validate_layout({**bare, "furniture": []})
        self.assertNotIn("screen", result)
        self.assertIsNone(screen_support(result))
        monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
        self.assertEqual(monitor["positionCm"], {"x": 100, "height": 110},
                         "without a screen the strip keeps its own place")
        hanging = validate_layout({**base, "screen": {**base["screen"], "on": None}})
        self.assertIsNone(screen_support(hanging))
        tv = {**base["screen"], "kind": "tv", "inches": 100}
        self.assertEqual(validate_layout({**base, "screen": tv})["screen"]["inches"], 100)
        for bad in ({**base["screen"], "on": "piece-0000000f"}, {**base["screen"], "inches": 56},
                    {**base["screen"], "kind": "projector"}, {**tv, "inches": 31}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_layout({**base, "screen": bad})

    def test_screen_fields_are_validated(self):
        for mutate in [
            lambda s: s.update(extra=1),
            lambda s: s.pop("inches"),
            lambda s: s.pop("positionCm"),
            lambda s: s.update(inches=18),
            lambda s: s.update(inches=56),
            lambda s: s.update(inches=True),
            lambda s: s.update(inches=float("nan")),
            lambda s: s["positionCm"].update(x=102),
            lambda s: s["positionCm"].update(height=301),
            lambda s: s["positionCm"].update(x=-205),
            lambda s: s["positionCm"].pop("height"),
            lambda s: s.update(positionCm=[100, 110]),
        ]:
            data = validate_layout(layout())
            mutate(data["screen"])
            with self.assertRaises(ValueError, msg=repr(data["screen"])):
                validate_layout(data)

    def test_sync_box_place_is_kept_and_checked(self):
        data = validate_layout(layout())
        self.assertNotIn("syncBox", data, "without a drag the box sits beside the stand")
        data["syncBox"] = {"x": 135}
        self.assertEqual(validate_layout(data)["syncBox"], {"x": 135})
        for bad in ({"x": 133}, {"x": "135"}, {"x": 135, "height": 85}, {"x": 900}, [135]):
            data["syncBox"] = bad
            with self.assertRaises(ValueError):
                validate_layout(data)

    def test_monitor_row_follows_screen(self):
        data = validate_layout(layout())
        data["screen"]["positionCm"] = {"x": 60, "height": 135}
        result = validate_layout(data)
        monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
        self.assertEqual(monitor["positionCm"], {"x": 60, "height": 135})
        self.assertEqual(result["screen"]["positionCm"], {"x": 60, "height": 135})
        # A low screen is raised, and the row follows the raised height.
        data["screen"]["inches"] = 55
        data["screen"]["positionCm"]["height"] = 20
        result = validate_layout(data)
        monitor = next(row for row in result["devices"] if row["id"] == "hue-monitor")
        self.assertEqual(result["screen"]["positionCm"]["height"], 40)
        self.assertEqual(monitor["positionCm"], result["screen"]["positionCm"])
        # The screen positions the other lights, not the row.
        moved = validate_layout(data)
        moved["devices"][2]["positionCm"] = {"x": 0, "height": 100}
        self.assertEqual(validate_layout(moved), result)

    def test_v7_round_trip(self):
        data = validate_layout(layout())
        self.assertEqual(validate_layout(copy.deepcopy(data)), data)
        with TemporaryDirectory(prefix="tmp_desk_layout_v7_") as folder:
            path = Path(folder) / "layout.json"
            store = LayoutStore(path)
            store.save(data)
            self.assertEqual(json.loads(path.read_text()), data)
            self.assertEqual(store.snapshot(), data)

    def test_screen_size_follows_the_sixteen_by_nine_diagonal(self):
        width, height = screen_size({"screen": {"inches": 32}})
        self.assertAlmostEqual(width * width + height * height, (32 * 2.54) ** 2)
        self.assertEqual(height, width * 9 / 16)

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

    def test_a_calibration_is_kept_on_govee_and_enrolled_device_rows_only(self):
        data = layout()
        calibration = {"warmth": 10, "tint": -5, "lowLight": 20}
        data["devices"][0]["calibration"] = dict(calibration)
        data["devices"].append({"id": "device-" + "c" * 32, "name": "Govee H6008",
                                "model": "H6008", "shape": "bar", "zones": 1, "scale": 80,
                                "positionCm": {"x": 40, "height": 30},
                                "calibration": dict(calibration)})
        rows = {row["id"]: row for row in validate_layout(data)["devices"]}
        self.assertEqual(rows["govee-left"]["calibration"], calibration)
        self.assertEqual(rows["device-" + "c" * 32]["calibration"], calibration)
        data["devices"][3]["calibration"] = dict(calibration)  # play-left
        with self.assertRaises(ValueError):
            validate_layout(data)

    def test_any_set_of_rows_is_valid_without_the_six_migrated_lamps(self):
        data = layout()
        data["version"] = 7
        del data["monitorInches"]
        data["screen"] = {"positionCm": {"x": 100, "height": 110}, "inches": 32}
        for keep in ([], ["play-left"], ["govee-right", "hue-monitor"]):
            with self.subTest(keep=keep):
                value = copy.deepcopy(data)
                value["devices"] = [row for row in value["devices"] if row["id"] in keep]
                result = validate_layout(value)
                self.assertEqual([row["id"] for row in result["devices"]], keep)
                self.assertEqual(result["screen"]["positionCm"], {"x": 100, "height": 110})
        # Before version seven the monitor row carries the screen, so it stays required.
        old = layout()
        old["devices"] = [row for row in old["devices"] if row["id"] != "hue-monitor"]
        with self.assertRaises(ValueError):
            validate_layout(old)

    def test_rejects_duplicates_nonfinite_values_and_control_fields(self):
        for mutate in [
            lambda d: d["devices"].append(copy.deepcopy(d["devices"][0])),
            lambda d: d["devices"][0].update(ip="192.168.10.123"),
            lambda d: d["devices"][0].update(scale=True),
            lambda d: d["devices"][0]["positionCm"].update(height=float("nan")),
        ]:
            data = layout()
            mutate(data)
            with self.assertRaises(ValueError):
                validate_layout(data)

    def test_older_versions_load_and_always_return_the_current_version(self):
        for version in (3, 4, 5, 6):
            data = layout()
            data["version"] = version
            with self.subTest(version=version):
                self.assertEqual(validate_layout(data)["version"], LAYOUT_VERSION)
        for version in (2, 8, "4", 4.0, True, None):
            data = layout()
            data["version"] = version
            with self.subTest(version=version), self.assertRaises(ValueError):
                validate_layout(data)

    def test_version_three_file_is_upgraded_on_the_next_save(self):
        with TemporaryDirectory(prefix="tmp_desk_layout_v3_") as folder:
            path = Path(folder) / "layout.json"
            path.write_text(json.dumps(layout()))
            store = LayoutStore(path)
            self.assertEqual(store.snapshot()["version"], LAYOUT_VERSION)
            store.save(store.snapshot())
            self.assertEqual(json.loads(path.read_text())["version"], LAYOUT_VERSION)

    def test_atomic_persistence_and_invalid_file_keep_last_good_configuration(self):
        with TemporaryDirectory(prefix="tmp_desk_layout_") as folder:
            path = Path(folder) / "layout.json"
            writer = LayoutStore(path)
            reader = LayoutStore(path)
            self.assertIsNone(reader.snapshot())
            writer.save(layout())
            self.assertEqual(reader.scale("govee-left", 1), 0.65)
            self.assertEqual(reader.scale("play-center", 1), 0.5)
            previous = reader.snapshot()
            path.write_text("{broken")
            self.assertEqual(reader.snapshot(), previous)
