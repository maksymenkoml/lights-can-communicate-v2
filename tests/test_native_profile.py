"""Validate the portable native Hue profile without devices or owner identities."""

import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from smart_lighting_core.native_profile import NativeHueProfile, NativeProfileError

STRIP = "a1111111-1111-4111-8111-111111111111"
LEFT = "b2222222-2222-4222-8222-222222222222"
CENTER = "c3333333-3333-4333-8333-333333333333"
RIGHT = "d4444444-4444-4444-8444-444444444444"
AREA = "e5555555-5555-4555-8555-555555555555"
CENTER_SERVICE = "f6666666-6666-4666-8666-666666666666"
LEFT_SERVICE = "a7777777-7777-4777-8777-777777777777"
RIGHT_SERVICE = "b8888888-8888-4888-8888-888888888888"
STRIP_SERVICE = "c9999999-9999-4999-8999-999999999999"
STRIP_ID = STRIP
PLAY_IDS = {"hue_left": LEFT, "hue_center": CENTER, "hue_right": RIGHT}
OTHER = "daaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
ZERO = "00000000-0000-0000-0000-000000000000"


def profile_data():
    """Return independently specified alternate resources in the current 4/12 shape."""
    return {
        "version": 1,
        "lights": {"strip": STRIP, "hue_left": LEFT, "hue_center": CENTER, "hue_right": RIGHT},
        "entertainment": {
            "area_id": AREA,
            "channels": [
                {"channel_id": 0, "member": {"rid": CENTER_SERVICE, "index": 0}},
                {"channel_id": 1, "member": {"rid": LEFT_SERVICE, "index": 0}},
                {"channel_id": 2, "member": {"rid": RIGHT_SERVICE, "index": 0}},
                {"channel_id": 3, "member": {"rid": STRIP_SERVICE, "index": 0}},
                {"channel_id": 4, "member": {"rid": STRIP_SERVICE, "index": 1}},
                {"channel_id": 5, "member": {"rid": STRIP_SERVICE, "index": 2}},
                {"channel_id": 6, "member": {"rid": STRIP_SERVICE, "index": 3}},
                {"channel_id": 7, "member": {"rid": STRIP_SERVICE, "index": 4}},
                {"channel_id": 8, "member": {"rid": STRIP_SERVICE, "index": 5}},
                {"channel_id": 9, "member": {"rid": STRIP_SERVICE, "index": 6}},
                {"channel_id": 10, "member": {"rid": STRIP_SERVICE, "index": 7}},
                {"channel_id": 11, "member": {"rid": STRIP_SERVICE, "index": 8}},
            ],
        },
    }


class NativeProfileTests(unittest.TestCase):
    def assert_invalid(self, data):
        with self.assertRaises(NativeProfileError) as caught:
            NativeHueProfile.from_dict(data)
        self.assertIsInstance(caught.exception, ValueError)
        for secret in (STRIP, AREA, CENTER_SERVICE, "private-value", "private-path"):
            self.assertNotIn(secret, str(caught.exception))

    def test_selection_and_members_use_exact_synthetic_resources(self):
        profile = NativeHueProfile.from_dict(profile_data())
        self.assertEqual(profile.strip_id, STRIP)
        self.assertEqual(
            dict(profile.play_ids), {"hue_left": LEFT, "hue_center": CENTER, "hue_right": RIGHT}
        )
        self.assertEqual(
            dict(profile.light_ids),
            {"strip": STRIP, "hue_left": LEFT, "hue_center": CENTER, "hue_right": RIGHT},
        )
        self.assertEqual(profile.area_id, AREA)
        self.assertEqual(
            profile.expected_members,
            (
                (CENTER_SERVICE, 0), (LEFT_SERVICE, 0), (RIGHT_SERVICE, 0),
                (STRIP_SERVICE, 0), (STRIP_SERVICE, 1), (STRIP_SERVICE, 2),
                (STRIP_SERVICE, 3), (STRIP_SERVICE, 4), (STRIP_SERVICE, 5),
                (STRIP_SERVICE, 6), (STRIP_SERVICE, 7), (STRIP_SERVICE, 8),
            ),
        )
        self.assertEqual(
            profile.resource_ids,
            frozenset((STRIP, LEFT, CENTER, RIGHT)),
        )

    def test_uppercase_uuids_are_canonical_and_channels_are_sorted(self):
        # Uppercase UUIDs only; field names stay exact and case-sensitive.
        data = profile_data()
        data["lights"] = {role: rid.upper() for role, rid in data["lights"].items()}
        data["entertainment"]["area_id"] = AREA.upper()
        for channel in data["entertainment"]["channels"]:
            channel["member"]["rid"] = channel["member"]["rid"].upper()
        data["entertainment"]["channels"].reverse()
        profile = NativeHueProfile.from_dict(data)
        self.assertEqual(profile.to_dict(), profile_data())
        self.assertEqual(json.dumps(profile.to_dict()), json.dumps(profile_data()))

    def test_input_and_returned_containers_cannot_change_the_profile(self):
        data = profile_data()
        profile = NativeHueProfile.from_dict(data)
        data["lights"]["strip"] = OTHER
        data["entertainment"]["channels"][0]["member"]["rid"] = OTHER
        exported = profile.to_dict()
        exported["lights"]["strip"] = OTHER
        exported["entertainment"]["channels"][0]["member"]["index"] = 9
        for name in ("light_ids", "play_ids"):
            returned = getattr(profile, name)
            try:
                returned["hue_left"] = OTHER
            except TypeError:
                pass
        with self.assertRaises((FrozenInstanceError, AttributeError, TypeError)):
            profile.area_id = OTHER
        with self.assertRaises(TypeError):
            profile.expected_members[0] = (OTHER, 0)
        self.assertEqual(profile.to_dict(), profile_data())

    def test_missing_or_unknown_fields_at_every_object_level_are_rejected(self):
        paths = [(), ("lights",), ("entertainment",),
                 ("entertainment", "channels", 0),
                 ("entertainment", "channels", 0, "member")]
        for path in paths:
            original = profile_data()
            original_target = original
            for part in path:
                original_target = original_target[part]
            for missing_key in (*original_target, None):
                data = copy.deepcopy(original)
                target = data
                for part in path:
                    target = target[part]
                if missing_key is not None:
                    del target[missing_key]
                else:
                    target["private-value"] = "private-value"
                with self.subTest(path=path, missing_key=missing_key):
                    self.assert_invalid(data)

    def test_version_is_exact_integer_one(self):
        for version in (True, False, 0, 2, 1.0, "1", None, [], {}):
            data = profile_data()
            data["version"] = version
            with self.subTest(version=version):
                self.assert_invalid(data)

    def test_malformed_containers_raise_safe_profile_errors(self):
        for bad in (None, [], (), "private-value", 7, True):
            with self.subTest(container="root", value=bad):
                self.assert_invalid(bad)
            for key in ("lights", "entertainment"):
                data = profile_data()
                data[key] = bad
                with self.subTest(container=key, value=bad):
                    self.assert_invalid(data)
            for path in (("channels",), ("channels", 0), ("channels", 0, "member")):
                data = profile_data()
                target = data["entertainment"]
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = bad
                with self.subTest(container=path, value=bad):
                    self.assert_invalid(data)

    def test_uuid_fields_reject_zero_invalid_and_non_string_values(self):
        paths = [("lights", role) for role in ("strip", "hue_left", "hue_center", "hue_right")]
        paths += [("entertainment", "area_id"),
                  ("entertainment", "channels", 0, "member", "rid")]
        for path in paths:
            for bad in (ZERO, "private-value", "", 1, True, None, [], {}):
                data = profile_data()
                target = data
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = bad
                with self.subTest(path=path, value=bad):
                    self.assert_invalid(data)

    def test_light_and_area_resources_must_be_distinct_after_normalization(self):
        for role in ("hue_left", "hue_center", "hue_right"):
            data = profile_data()
            data["lights"][role] = STRIP.upper()
            with self.subTest(role=role):
                self.assert_invalid(data)
        for rid in (STRIP, LEFT, CENTER, RIGHT):
            data = profile_data()
            data["entertainment"]["area_id"] = rid
            with self.subTest(area=rid):
                self.assert_invalid(data)

    def test_entertainment_services_cannot_overlap_other_resources(self):
        services = (CENTER_SERVICE, LEFT_SERVICE, RIGHT_SERVICE, STRIP_SERVICE)
        for channel_id in range(4):
            for rid in (STRIP, LEFT, CENTER, RIGHT, AREA, *services):
                if rid == services[channel_id]:
                    continue
                data = profile_data()
                channels = data["entertainment"]["channels"]
                channels[channel_id]["member"]["rid"] = rid.upper()
                if channel_id == 3:
                    for channel in channels[4:]:
                        channel["member"]["rid"] = rid
                with self.subTest(channel_id=channel_id, rid=rid):
                    self.assert_invalid(data)

    def test_channel_count_ids_and_integer_types_are_strict(self):
        for length in (0, 11, 13):
            data = profile_data()
            channels = data["entertainment"]["channels"]
            data["entertainment"]["channels"] = (channels + [copy.deepcopy(channels[0])])[:length]
            with self.subTest(length=length):
                self.assert_invalid(data)
        for bad in (True, False, -1, 12, 1, 0.0, "0", None, [], {}):
            data = profile_data()
            data["entertainment"]["channels"][0]["channel_id"] = bad
            with self.subTest(channel_id=bad):
                self.assert_invalid(data)

    def test_indexes_and_strip_membership_preserve_exact_mapping(self):
        for channel_id in range(12):
            for bad in (True, False, -1, 9, 0.0, "0", None, [], {}):
                data = profile_data()
                data["entertainment"]["channels"][channel_id]["member"]["index"] = bad
                with self.subTest(channel_id=channel_id, index=bad):
                    self.assert_invalid(data)
            data = profile_data()
            data["entertainment"]["channels"][channel_id]["member"]["index"] = 1
            if channel_id != 4:
                with self.subTest(channel_id=channel_id, index=1):
                    self.assert_invalid(data)
        for channel_id in range(4, 12):
            data = profile_data()
            data["entertainment"]["channels"][channel_id]["member"]["rid"] = OTHER
            with self.subTest(channel_id=channel_id, split_service=True):
                self.assert_invalid(data)

    def test_load_round_trip_and_exact_byte_limit(self):
        encoded = json.dumps(profile_data()).encode("utf-8")
        with tempfile.TemporaryDirectory(prefix="tmp_native_profile_") as directory:
            path = Path(directory) / "profile.json"
            path.write_bytes(encoded + b" " * (16 * 1024 - len(encoded)))
            self.assertEqual(NativeHueProfile.load(path).to_dict(), profile_data())
            self.assertEqual(NativeHueProfile.load(str(path)).to_dict(), profile_data())
            path.write_bytes(encoded + b" " * (16 * 1024 + 1 - len(encoded)))
            with self.assertRaises(NativeProfileError):
                NativeHueProfile.load(path)

    def test_load_errors_never_echo_path_or_file_values(self):
        with tempfile.TemporaryDirectory(prefix="tmp_native_profile_") as directory:
            path = Path(directory) / "private-path.json"
            payloads = [b"private-value", b"\xff", b"[", b"[]",
                        b"{", b'{"private-value": NaN}', b"[" * 1200]
            malformed_profile = profile_data()
            malformed_profile["lights"]["strip"] = "private-value"
            payloads.append(json.dumps(malformed_profile).encode("utf-8"))
            for payload in payloads:
                path.write_bytes(payload)
                with self.subTest(payload=payload[:30]):
                    with self.assertRaises(NativeProfileError) as caught:
                        NativeHueProfile.load(path)
                    self.assertNotIn("private", str(caught.exception))
                    self.assertNotIn(str(path), str(caught.exception))
            path.unlink()
            for missing in (path, Path(directory), "bad\x00private-path", None, [], {}):
                with self.subTest(path_type=type(missing)):
                    with self.assertRaises(NativeProfileError) as caught:
                        NativeHueProfile.load(missing)
                    self.assertNotIn("private", str(caught.exception))

    def test_duplicate_json_fields_at_every_level_fail_even_when_values_match(self):
        valid = json.dumps(profile_data())
        replacements = [
            ('"version": 1', '"version": 1, "version": 1'),
            (f'"strip": "{STRIP}"', f'"strip": "{STRIP}", "strip": "{STRIP}"'),
            (f'"area_id": "{AREA}"', f'"area_id": "{AREA}", "area_id": "{AREA}"'),
            ('"channel_id": 0', '"channel_id": 0, "channel_id": 0'),
            ('"index": 0', '"index": 0, "index": 0'),
        ]
        with tempfile.TemporaryDirectory(prefix="tmp_native_profile_") as directory:
            path = Path(directory) / "profile.json"
            for original, duplicate in replacements:
                path.write_text(valid.replace(original, duplicate, 1), encoding="utf-8")
                with self.subTest(field=original):
                    with self.assertRaises(NativeProfileError):
                        NativeHueProfile.load(path)


if __name__ == "__main__":
    unittest.main()
