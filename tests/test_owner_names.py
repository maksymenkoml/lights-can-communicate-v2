"""Owner names from Light Studio rename the virtual Hue lights, without vendor startup."""
import ast
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiments/hue-bridge"))
from hue_names import SLOT_ROLES, OwnerNames  # noqa: E402


def light(slot, name):
    return types.SimpleNamespace(id_v1=slot, id_v2="id-" + slot, name=name)


class Fixture:
    def __init__(self):
        self.lights = {"1": light("1", "Govee Left"), "2": light("2", "Govee Right"),
                       "3": light("3", "Hue Monitor")}
        self.names = {}
        self.saves = 0
        self.published = []
        self.owner = OwnerNames(
            self.lights, {"1": "left", "2": "right", "3": "strip"}, lambda: dict(self.names),
            save=self.save, publish=self.published.append)

    def save(self):
        self.saves += 1


class OwnerNamesTests(unittest.TestCase):
    def test_without_an_owner_name_nothing_is_renamed_saved_or_published(self):
        fixture = Fixture()
        self.assertEqual(fixture.owner.apply(), [])
        self.assertEqual([lamp.name for lamp in fixture.lights.values()],
                         ["Govee Left", "Govee Right", "Hue Monitor"])
        self.assertEqual((fixture.saves, fixture.published), (0, []))

    def test_start_applies_names_and_keeps_resource_identity(self):
        fixture = Fixture()
        fixture.names = {"left": "Desk left", "strip": "Monitor bar", "hue_left": "No light"}
        self.assertEqual(fixture.owner.apply(announce=False), ["1", "3"])
        self.assertEqual([lamp.name for lamp in fixture.lights.values()],
                         ["Desk left", "Govee Right", "Monitor bar"])
        self.assertEqual([lamp.id_v2 for lamp in fixture.lights.values()],
                         ["id-1", "id-2", "id-3"])
        self.assertEqual((fixture.saves, fixture.published), (1, []))

    def test_a_layout_change_renames_once_saves_and_publishes_the_changed_lights(self):
        fixture = Fixture()
        fixture.names = {"left": "Desk left"}
        fixture.owner.apply(announce=False)
        for _ in range(3):
            self.assertEqual(fixture.owner.apply(), [])
        self.assertEqual((fixture.saves, fixture.published), (1, []))
        fixture.names = {"left": "Desk left", "right": "Desk right"}
        self.assertEqual(fixture.owner.apply(), ["2"])
        self.assertEqual(fixture.published, [[fixture.lights["2"]]])
        self.assertEqual(fixture.saves, 2)
        fixture.names = {"left": "Floor lamp", "right": "Desk right"}
        self.assertEqual(fixture.owner.apply(), ["1"])
        self.assertEqual(fixture.lights["1"].name, "Floor lamp")

    def test_a_removed_owner_name_keeps_the_name_and_a_hue_app_rename_stays(self):
        fixture = Fixture()
        fixture.names = {"left": "Desk left"}
        fixture.owner.apply()
        fixture.lights["1"].name = "Chosen in Hue app"
        fixture.owner.apply()
        self.assertEqual(fixture.lights["1"].name, "Chosen in Hue app")
        fixture.names = {}
        fixture.owner.apply()
        self.assertEqual(fixture.lights["1"].name, "Chosen in Hue app")
        # The same name again after a removal is a new Light Studio change.
        fixture.names = {"left": "Desk left"}
        self.assertEqual(fixture.owner.apply(), [])

    def test_a_light_that_already_has_the_name_is_not_saved_or_published(self):
        fixture = Fixture()
        fixture.names = {"right": "Govee Right"}
        self.assertEqual(fixture.owner.apply(), [])
        self.assertEqual((fixture.saves, fixture.published), (0, []))

    def test_slot_roles_match_the_bridge_output_mapping(self):
        source = (ROOT / "experiments/hue-bridge/bridge_output.py").read_text()
        for slot, role in SLOT_ROLES.items():
            self.assertIn(f'"{slot}"', source)
            self.assertIn(f'"{role}"', source)
        from bridge_output import BridgeOutput

        output = BridgeOutput("http://127.0.0.1:1", "t", include_strip=True,
                              include_hue_play=True)
        self.assertEqual(output.slots, SLOT_ROLES)


class BootstrapAnnouncementTests(unittest.TestCase):
    def test_renamed_lights_are_announced_as_a_v2_update_with_their_metadata(self):
        source = ROOT / "experiments/hue-bridge/bootstrap.py"
        tree = ast.parse(source.read_text())
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "publish_light_names")
        events = []
        namespace = {"StreamEvent": events.append, "time": __import__("time"),
                     "uuid": __import__("uuid")}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
             namespace)
        lamp = types.SimpleNamespace(getV2Api=lambda: {
            "id": "id-1", "id_v1": "/lights/1", "type": "light", "on": {"on": True},
            "metadata": {"name": "Desk left", "archetype": "sultan_bulb"}})
        namespace["publish_light_names"]([lamp])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "update")
        self.assertEqual(events[0]["data"], [{
            "id": "id-1", "id_v1": "/lights/1", "type": "light",
            "metadata": {"name": "Desk left", "archetype": "sultan_bulb"}}])

    def test_bootstrap_applies_names_at_start_and_watches_changes(self):
        text = (ROOT / "experiments/hue-bridge/bootstrap.py").read_text()
        self.assertIn("owner_names.apply(announce=False)", text)
        self.assertIn("name='desk-names'", text)
        self.assertIn("layout_store.names", text)


if __name__ == "__main__":
    unittest.main()
