"""Room management in Light Studio (#78 stage 4)."""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desk_layout import validate_layout  # noqa: E402
from rooms import FIRST_ROOM  # noqa: E402
from test_desk_layout import layout  # noqa: E402
from test_light_registry import sample  # noqa: E402
from test_setup_live_status import status  # noqa: E402
from test_setup_studio import studio  # noqa: E402

from lights_can_communicate_v2_core.light_registry import LightRegistry  # noqa: E402


class StudioRoomTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_studio_rooms_")
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        data = validate_layout(layout())
        data["room"] = "Office"
        (self.state / "desk-layout.json").write_text(json.dumps(data))
        LightRegistry.from_dict(sample()).save(self.state / "lights.json")
        self.calls, self.syncing = [], False
        self.answers = {"/lights/rooms": (200, {"accepted": True, "restarting": False,
                                                "changes": {}}),
                        "/lights/enroll": (200, {"accepted": True, "restarting": True})}

        def post(route, body):
            self.calls.append((route, body))
            return self.answers[route]

        def live():
            if self.syncing is None:
                raise OSError("adapter unavailable")
            data = status()
            if self.syncing == "no-video":
                del data["video"]
            elif self.syncing:
                data["video"].update(active=True, ready=True, frame_age=0.1)
                data["preview"]["mode"] = "sync"
            return data

        server = ThreadingHTTPServer(("127.0.0.1", 0), studio.handler_for(
            self.state / "missing-token", self.state, lights_post=post, live_fetch=live,
            source_fetch=lambda: True))
        worker = threading.Thread(target=server.serve_forever)
        worker.start()
        self.base = "http://127.0.0.1:" + str(server.server_port)

        def close():
            server.shutdown()
            server.server_close()
            worker.join(2)

        self.addCleanup(close)

    def request(self, path, body=None, **headers):
        request = urllib.request.Request(
            self.base + path, data=None if body is None else json.dumps(body).encode(),
            method="GET" if body is None else "POST",
            headers={"Content-Type": "application/json", "Origin": self.base, **headers})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as response:
            with response:
                return response.code, json.load(response)

    def test_rooms_list_their_names_the_sync_box_and_the_lights(self):
        status_, body = self.request("/api/rooms")
        self.assertEqual(status_, 200)
        self.assertEqual(body["rooms"], [{"id": FIRST_ROOM, "name": "Office"}])
        self.assertEqual(body["sync"], FIRST_ROOM)
        self.assertEqual(set(body["lights"].values()), {FIRST_ROOM})
        self.assertEqual(len(body["lights"]), 6)
        # Every light row says where it is; nothing is recorded before a room change.
        self.assertEqual(set(body["placed"].values()), {FIRST_ROOM})
        self.assertIn("play-left", body["placed"])
        self.assertEqual(body["assigned"], {})
        room = self.request("/api/rooms/create", {"name": "Bedroom"})[1]["rooms"][1]["id"]
        body = self.request("/api/rooms/move", {"light": "govee-left", "room": room})[1]
        self.assertEqual((body["assigned"], body["placed"]["govee-left"]),
                         ({"govee-left": room}, room))

    def test_a_new_room_has_its_own_layout(self):
        status_, body = self.request("/api/rooms/create", {"name": "Bedroom"})
        self.assertEqual(status_, 200)
        room = body["rooms"][1]["id"]
        self.assertEqual(body["rooms"][1]["name"], "Bedroom")
        status_, answer = self.request("/api/layout?room=" + room)
        self.assertEqual((status_, answer["layout"]["devices"]), (200, []))
        layout_ = answer["layout"]
        layout_["desk"]["width"] = 120
        status_, saved = self.request("/api/layout?room=" + room, layout_,
                                      **{"If-Match": answer["revision"]})
        self.assertEqual((status_, saved["layout"]["desk"]["width"]), (200, 120))
        # The first room is unchanged and stays the default.
        self.assertEqual(self.request("/api/layout")[1]["layout"]["desk"]["width"], 160)
        self.assertEqual(self.request("/api/layout?room=room-ffffffff")[0], 404)
        # Renaming and reordering are allowed during Sync; creating is not.
        self.syncing = True
        self.assertEqual(self.request("/api/rooms/rename", {"room": room, "name": "Den"})[0],
                         200)
        status_, body = self.request("/api/rooms/order", {"rooms": [room, FIRST_ROOM]})
        self.assertEqual([item["name"] for item in body["rooms"]], ["Den", "Office"])
        self.assertEqual(self.request("/api/rooms/create", {"name": "Hall"}),
                         (409, {"accepted": False, "reason": "sync_active"}))
        self.assertEqual(self.calls, [])

    def test_moving_a_light_lets_the_area_follow_and_a_refusal_undoes_it(self):
        room = self.request("/api/rooms/create", {"name": "Bedroom"})[1]["rooms"][1]["id"]
        status_, body = self.request("/api/rooms/move", {"light": "play-left", "room": room})
        self.assertEqual((status_, body["lights"]["play-left"]), (200, room))
        self.assertEqual(self.calls, [("/lights/rooms", {})])
        # A Govee light has no area: its move does not wait for the adapter.
        self.answers["/lights/rooms"] = (503, {})
        self.assertEqual(self.request("/api/rooms/move", {"light": "govee-right",
                                                          "room": room})[0], 200)
        self.assertEqual(len(self.calls), 1)
        self.answers["/lights/rooms"] = (409, {"accepted": False, "reason": "sync_active"})
        self.assertEqual(self.request("/api/rooms/move", {"light": "play-right", "room": room}),
                         (409, {"accepted": False, "reason": "sync_active"}))
        self.assertEqual(self.request("/api/rooms")[1]["lights"]["play-right"], FIRST_ROOM)
        rows = self.request("/api/layout")[1]["layout"]["devices"]
        self.assertIn("play-right", [row["id"] for row in rows])
        self.assertNotIn("play-left", [row["id"] for row in rows])
        # The Sync Box moves the same way.
        self.answers["/lights/rooms"] = (200, {"accepted": True, "changes": {}})
        status_, body = self.request("/api/rooms/sync-box", {"room": room})
        self.assertEqual((status_, body["sync"]), (200, room))
        self.syncing = True
        self.assertEqual(self.request("/api/rooms/move", {"light": "play-left",
                                                          "room": FIRST_ROOM})[1]["reason"],
                         "sync_active")

    def test_room_refusals(self):
        room = self.request("/api/rooms/create", {"name": "Bedroom"})[1]["rooms"][1]["id"]
        self.request("/api/rooms/move", {"light": "govee-left", "room": room})
        self.assertEqual(self.request("/api/rooms/delete", {"room": room})[1]["reason"],
                         "has_lights")
        self.assertEqual(self.request("/api/rooms/delete", {"room": FIRST_ROOM})[1]["reason"],
                         "has_sync_box")
        self.assertEqual(self.request("/api/rooms/create", {"name": ""})[1]["reason"],
                         "invalid_name")
        self.assertEqual(self.request("/api/rooms/move", {"light": "lamp", "room": room})[1]
                         ["reason"], "unknown_light")
        self.assertEqual(self.request("/api/rooms/rename", {"room": room})[0], 400)
        self.request("/api/rooms/move", {"light": "govee-left", "room": FIRST_ROOM})
        status_, body = self.request("/api/rooms/delete", {"room": room})
        self.assertEqual((status_, [item["id"] for item in body["rooms"]]), (200, [FIRST_ROOM]))

    def test_unknown_sync_state_or_registry_refuses_the_risky_changes(self):
        room = self.request("/api/rooms/create", {"name": "Bedroom"})[1]["rooms"][1]["id"]
        self.syncing = None
        unavailable = (503, {"accepted": False, "reason": "unavailable"})
        self.assertEqual(self.request("/api/rooms/create", {"name": "Hall"}), unavailable)
        self.assertEqual(self.request("/api/rooms/move", {"light": "govee-left",
                                                          "room": room}), unavailable)
        self.assertEqual(self.request("/api/rooms/rename", {"room": room, "name": "Den"})[0],
                         200)
        # An adapter without Sync output has nothing that Sync could own.
        self.syncing = "no-video"
        hall = self.request("/api/rooms/create", {"name": "Hall"})
        self.assertEqual(hall[0], 200)
        self.syncing = False
        self.assertEqual(self.request("/api/rooms/delete",
                                      {"room": hall[1]["rooms"][2]["id"]})[0], 200)
        for broken in ("{not json", '{"lights": {}}', '{"lights": [{"id": "lamp"}]}', "[]"):
            (self.state / "lights.json").write_text(broken)
            self.assertEqual(self.request("/api/rooms/delete", {"room": room}), unavailable)
        self.assertEqual(self.request("/api/rooms/move", {"light": "play-left",
                                                          "room": room}), unavailable)
        self.assertEqual(len(self.request("/api/rooms")[1]["rooms"]), 2)
        (self.state / "lights.json").unlink()
        self.assertEqual(self.request("/api/rooms/delete", {"room": room})[0], 200)

    def test_a_refused_sync_box_move_restores_the_single_layout(self):
        self.answers["/lights/rooms"] = (409, {"accepted": False, "reason": "sync_active"})
        self.assertEqual(self.request("/api/rooms/sync-box", {"room": None})[1]["reason"],
                         "sync_active")
        self.assertFalse((self.state / "rooms.json").exists())
        self.assertTrue((self.state / "desk-layout.json").exists())
        self.assertEqual(self.request("/api/rooms")[1]["sync"], FIRST_ROOM)

    def test_an_enrolled_light_joins_the_named_room(self):
        room = self.request("/api/rooms/create", {"name": "Bedroom"})[1]["rooms"][1]["id"]
        light = "device-" + "c" * 32
        self.assertEqual(self.request("/api/lights/enroll", {"id": light, "room": room})[0], 200)
        index = json.loads((self.state / "rooms.json").read_text())
        self.assertEqual(index["lights"][light], room)
        # A refused enrollment leaves no room behind; a migrated lamp keeps its id.
        other = "device-" + "d" * 32
        self.answers["/lights/enroll"] = (409, {"accepted": False, "reason": "unavailable"})
        self.assertEqual(self.request("/api/lights/enroll", {"id": other, "room": room})[0], 409)
        self.assertNotIn(other, json.loads((self.state / "rooms.json").read_text())["lights"])
        self.answers["/lights/enroll"] = (200, {"accepted": True, "restarting": True,
                                                "id": "play-left"})
        self.request("/api/lights/enroll", {"id": other, "room": room})
        self.assertEqual(json.loads((self.state / "rooms.json").read_text())["lights"]
                         ["play-left"], room)
        self.assertEqual(self.request("/api/lights/enroll",
                                      {"id": other, "room": "room-ffffffff"})[1]["reason"],
                         "unknown_room")


if __name__ == "__main__":
    unittest.main()
