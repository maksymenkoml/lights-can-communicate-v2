import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiments/hue-bridge"))
from hue_resource_compat import (  # noqa: E402
    canonical_collection,
    canonical_light,
    install_resource_compat,
)


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.bridge_id = "020000FFFE000000"
        self.home_id = str(uuid.uuid5(uuid.NAMESPACE_URL, self.bridge_id + "bridge_home"))
        self.api = SimpleNamespace(
            v2BridgeEntertainment=lambda: {
                "id": "old-unrelated-id",
                "type": "entertainment",
                "proxy": True,
                "max_streams": 1,
            },
            v2BridgeHome=lambda: {
                "id": self.home_id,
                "services": [
                    {"rid": "legacy-sensor-without-v2-device", "rtype": "device"},
                    {"rid": "known-light-service", "rtype": "light"},
                ],
            },
            geoLocation=lambda: {
                "type": "geolocation",
                "is_configured": False,
                "sun_today": {"day_type": "normal_day", "sunset_time": ""},
            },
        )
        self.groups, self.events = {}, []
        event_module = SimpleNamespace(StreamEvent=lambda message: self.events.append(message))
        groups = self.groups

        class Group:
            def __init__(self, kind, slot):
                self.type, self.id_v1 = kind, slot
                self.id_v2 = str(uuid.uuid5(uuid.NAMESPACE_URL, "group-" + slot))
                groups[slot] = self

            def getV2GroupedLight(self):
                return {
                    "id": self.id_v2,
                    "type": "grouped_light",
                    "on": {"on": True},
                    "owner": {"rid": "private-api-username", "rtype": "device"},
                }

            def getV2Room(self):
                return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, self.id_v2 + "room"))}

            def getV2Zone(self):
                return {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, self.id_v2 + "zone"))}

            def genStreamEvent(self, state):
                event_module.StreamEvent(
                    {
                        "type": "update",
                        "data": [
                            {
                                "id": self.id_v2,
                                "type": "grouped_light",
                                **state,
                                "owner": {"rid": self.getV2Zone()["id"], "rtype": "zone"},
                            }
                        ],
                    }
                )

        self.group_class = Group
        install_resource_compat(self.api, Group, self.bridge_id, self.groups, event_module)

    def test_grouped_light_owner_is_its_room_or_zone_without_exposing_api_username(self):
        for kind, method in (("Room", "getV2Room"), ("Zone", "getV2Zone")):
            group = self.group_class(kind, "1")
            result = group.getV2GroupedLight()
            self.assertEqual(
                result["owner"], {"rid": getattr(group, method)()["id"], "rtype": kind.lower()}
            )
            self.assertNotIn("private-api-username", json.dumps(result))
            self.assertTrue(result["on"]["on"])

    def test_all_lights_owner_is_existing_bridge_home(self):
        result = self.group_class("LightGroup", "0").getV2GroupedLight()
        self.assertEqual(result["owner"], {"rid": self.home_id, "rtype": "bridge_home"})

    def test_bridge_entertainment_id_matches_advertised_service_reference(self):
        result = self.api.v2BridgeEntertainment()
        expected = str(uuid.uuid5(uuid.NAMESPACE_URL, self.bridge_id + "entertainment"))
        self.assertEqual(result["id"], expected)
        self.assertTrue(result["proxy"])
        self.assertEqual(result["max_streams"], 1)

    def test_bridge_home_services_do_not_advertise_legacy_sensor_as_a_device(self):
        result = self.api.v2BridgeHome()
        self.assertEqual(result["services"], [{"rid": "known-light-service", "rtype": "light"}])

    def test_grouped_light_sse_owner_matches_the_public_resource(self):
        group = self.group_class("LightGroup", "0")
        group.genStreamEvent({"on": {"on": True}})
        emitted = self.events[0]["data"][0]
        self.assertEqual(emitted["owner"], group.getV2GroupedLight()["owner"])
        self.assertTrue(emitted["on"]["on"])

    def test_canonical_collection_excludes_vendor_extension_and_its_key(self):
        light = {"id": "light", "type": "light"}
        payload = {
            "errors": [],
            "data": [
                light,
                {
                    "id": "vendor",
                    "type": "diyhue",
                    "hue_essentials_key": "private-key",
                },
            ],
        }
        result = canonical_collection(payload)
        self.assertEqual(result, {"errors": [], "data": [light]})
        self.assertNotIn("private-key", json.dumps(result))
        self.assertEqual(len(payload["data"]), 2)

    def test_unconfigured_geolocation_does_not_publish_an_invalid_empty_time(self):
        result = self.api.geoLocation()
        self.assertFalse(result["is_configured"])
        self.assertNotIn("sun_today", result)

    def test_rgb_light_cannot_mark_null_mirek_as_valid(self):
        source = {"type": "light", "color_temperature": {"mirek": None, "mirek_valid": True}}
        result = canonical_light(source)
        self.assertFalse(result["color_temperature"]["mirek_valid"])
        self.assertTrue(source["color_temperature"]["mirek_valid"])
        white = {"type": "light", "color_temperature": {"mirek": 350, "mirek_valid": True}}
        self.assertEqual(canonical_light(white), white)

    def test_empty_timed_effects_has_a_complete_inactive_capability(self):
        result = canonical_light({"type": "light", "timed_effects": {}})
        self.assertEqual(
            result["timed_effects"],
            {"status": "no_effect", "status_values": ["no_effect"], "effect_values": ["no_effect"]},
        )

    def test_v1_colorloop_is_not_advertised_as_a_v2_effect(self):
        result = canonical_light(
            {
                "type": "light",
                "effects": {
                    "status": "colorloop",
                    "status_values": ["no_effect", "colorloop"],
                    "effect_values": ["no_effect", "candle", "colorloop"],
                },
            }
        )
        self.assertEqual(result["effects"]["status"], "no_effect")
        self.assertEqual(result["effects"]["effect_values"], ["no_effect", "candle"])
        self.assertEqual(result["effects"]["status_values"], ["no_effect"])
