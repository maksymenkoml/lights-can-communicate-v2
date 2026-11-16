import importlib.util
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

spec = importlib.util.spec_from_file_location(
    "setup_studio", Path(__file__).resolve().parents[1] / "experiments/setup-studio/server.py"
)
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)


def catalog_device(**changes):
    return {
        "id": "device-" + "a" * 32, "provider": "govee", "name": "Third lamp",
        "model": "H6076", "shape": "floor", "zones": 7, "supported": True,
        "online": True, "selectable": True, "ambiguous": False, "layout_id": None,
        "height_cm": 136, "width_cm": None, **changes,
    }


def placement(device=None):
    item = device or catalog_device()
    return {key: item[key] for key in ("id", "name", "model", "shape", "zones")} | {
        "scale": 80, "orientation": "horizontal", "positionCm": {"x": 100, "height": 60}
    }


@contextmanager
def discovery_server(fetch):
    with TemporaryDirectory(prefix="tmp_studio_discovery_") as folder:
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            studio.handler_for(Path(folder) / "missing-token", catalog_fetch=fetch),
        )
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path="/api/discovery", body=None, **headers):
            data = None if body is None else json.dumps(body).encode()
            try:
                with opener.open(urllib.request.Request(
                    base + path, data=data,
                    headers={"Content-Type": "application/json", "Origin": base, **headers},
                )) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                error.close()
                raise

        try:
            yield request
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


class StudioTests(unittest.TestCase):
    def test_zone_survives_older_client_save_and_explicit_null_resets_it(self):
        from test_desk_layout import layout
        from test_floor_zone_contract import custom_zone

        with discovery_server(lambda: {}) as request:
            data = layout()
            data['syncZone'] = custom_zone()
            saved = request('/api/layout', data)
            older = layout()
            older['desk']['width'] = 180
            saved = request('/api/layout', older, **{'If-Match': saved['revision']})
            self.assertEqual(saved['layout']['syncZone'], data['syncZone'])
            older['syncZone'] = None
            reset = request('/api/layout', older, **{'If-Match': saved['revision']})
            self.assertNotIn('syncZone', reset['layout'])
            self.assertEqual(request('/api/layout'), reset)

    def test_discovery_proxy_preserves_public_metadata_and_hides_private_fields(self):
        snapshot = {"connected": True, "devices": [catalog_device(ip="private-ip")],
                    "providers": {"govee": "ready", "hue": "ready"}, "token": "secret"}
        with discovery_server(lambda: snapshot) as request:
            result = request()
            self.assertEqual(result["devices"], [catalog_device()])
            self.assertEqual(result["providers"], {"govee": "ready", "hue": "ready"})
            self.assertNotIn("private-ip", json.dumps(result))
            self.assertNotIn("secret", json.dumps(result))
            with self.assertRaises(urllib.error.HTTPError) as error:
                request(Origin="https://unrelated.example")
            self.assertEqual(error.exception.code, 403)

    def test_discovery_failures_are_recoverable_and_do_not_expose_exception_text(self):
        body = io.BytesIO(b"secret-body")
        for failure in (OSError("secret-token private-address"), RuntimeError("secret-callback"),
                        urllib.error.HTTPError("http://private/", 401, "secret-auth", {}, body)):
            def unavailable():
                raise failure
            with self.subTest(failure=type(failure)), discovery_server(unavailable) as request:
                result = request()
                self.assertFalse(result["connected"])
                self.assertEqual(result["devices"], [])
                self.assertNotIn("secret", json.dumps(result))
        self.assertTrue(body.closed)

    def test_discovery_projection_rejects_nested_metadata_and_keeps_provider_states(self):
        snapshot = {"connected": True, "devices": [
            catalog_device(name={"secret": "private-token"}),
            catalog_device(id="device-" + "b" * 32, supported=False, selectable=False,
                           shape=None, zones=None, model="plug", height_cm=None),
        ], "providers": {"govee": "discovering", "hue": "not_configured"}}
        with discovery_server(lambda: snapshot) as request:
            result = request()
            self.assertEqual(len(result["devices"]), 1)
            self.assertIsNone(result["devices"][0]["shape"])
            self.assertEqual(result["providers"],
                             {"govee": "discovering", "hue": "not_configured"})
            self.assertNotIn("private-token", json.dumps(result))

    def test_malformed_layout_posts_return_validation_errors_without_losing_saved_geometry(self):
        from test_desk_layout import layout

        with discovery_server(lambda: {}) as request:
            saved = request("/api/layout", layout())
            for body in (["bad"], {"devices": [None]}):
                with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                    request("/api/layout", body)
                self.assertEqual(error.exception.code, 400)
                self.assertEqual(request("/api/layout"), saved)

    def test_only_selectable_canonical_new_bindings_are_accepted_and_saved_rows_edit_offline(self):
        from test_desk_layout import layout

        state = {"connected": True, "devices": [catalog_device()],
                 "providers": {"govee": "ready", "hue": "ready"}}
        def fetch():
            if state is None:
                raise OSError("offline")
            return state
        with discovery_server(fetch) as request:
            initial = request("/api/layout", layout())
            data = layout()
            data["devices"].append(placement())
            for mutation in ("unknown", "metadata", "offline", "ambiguous", "legacy", "duplicate"):
                candidate = json.loads(json.dumps(data))
                state["devices"] = [catalog_device()]
                if mutation == "unknown":
                    candidate["devices"][-1]["id"] = "device-" + "b" * 32
                elif mutation == "metadata":
                    candidate["devices"][-1]["zones"] = 1
                elif mutation == "offline":
                    state["devices"][0].update(online=False, selectable=False)
                elif mutation == "ambiguous":
                    state["devices"][0].update(ambiguous=True)
                elif mutation == "legacy":
                    state["devices"][0]["layout_id"] = "govee-left"
                else:
                    candidate["devices"].append(placement())
                with self.subTest(mutation=mutation), self.assertRaises(urllib.error.HTTPError):
                    request("/api/layout", candidate, **{"If-Match": initial["revision"]})
                self.assertEqual(request("/api/layout"), initial)
            state["devices"] = [catalog_device()]
            saved = request("/api/layout", data, **{"If-Match": initial["revision"]})
            self.assertEqual(saved["layout"]["devices"][-1], placement())
            state = None
            data["devices"][-1]["scale"] = 35
            edited = request("/api/layout", data, **{"If-Match": saved["revision"]})
            self.assertEqual(edited["layout"]["devices"][-1]["scale"], 35)
            data["devices"][-1]["model"] = "forged"
            with self.assertRaises(urllib.error.HTTPError):
                request("/api/layout", data)
            data["devices"].pop()
            self.assertEqual(len(request("/api/layout", data)["layout"]["devices"]), 6)

    def test_layout_api_persists_geometry_and_rejects_cross_origin_or_stale_writes(self):
        from test_desk_layout import layout

        with TemporaryDirectory(prefix="tmp_studio_layout_") as folder:
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), studio.handler_for(Path(folder) / "missing-token")
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            base = "http://127.0.0.1:" + str(server.server_port)
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            def post(body, **headers):
                return opener.open(
                    urllib.request.Request(
                        base + "/api/layout",
                        data=json.dumps(body).encode(),
                        method="POST",
                        headers={"Content-Type": "application/json", "Origin": base, **headers},
                    )
                )

            try:
                with opener.open(base + "/api/layout") as response:
                    self.assertIsNone(json.load(response)["layout"])
                with post(layout()) as response:
                    result = json.load(response)
                self.assertEqual(result["layout"]["desk"]["width"], 160)
                with opener.open(base + "/api/layout") as response:
                    self.assertEqual(json.load(response), result)
                for headers, code in [
                    ({"Origin": "https://unrelated.example"}, 403),
                    ({"Host": "unrelated.example"}, 403),
                    ({"If-Match": "stale-revision"}, 409),
                ]:
                    with (
                        self.subTest(headers=headers),
                        self.assertRaises(urllib.error.HTTPError) as e,
                    ):
                        post(layout(), **headers)
                    self.assertEqual(e.exception.code, code)
                    e.exception.close()
                bad = layout()
                bad["devices"][0]["ip"] = "192.168.10.123"
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post(bad)
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                self.assertEqual(
                    json.loads((Path(folder) / "desk-layout.json").read_text()), result["layout"]
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_status_projection_excludes_keys_unselected_devices_and_raw_commands(self):
        result = studio.project_status(
            {
                "hue_key": "secret-key",
                "video": {"active": True, "ready": True, "frame_age": .1,
                          "session": "private-session"},
                "targets": [
                    {
                        "side": "left",
                        "status_age": .1,
                        "native": {
                            "identity_verified": True,
                            "onOff": 1,
                            "brightness": 51,
                            "secret": "secret-token",
                        },
                    },
                    {"side": "third", "native": {"onOff": 1}},
                ],
                "hue_play": {
                    "hue_center": {
                        "healthy": True,
                        "native_age": .1,
                        "native_on": True,
                        "native_brightness": 39,
                        "last_payload": {"sensitive": "secret-body"},
                    }
                },
            }
        )
        encoded = json.dumps(result)
        self.assertEqual(result["mode"], "sync")
        self.assertEqual(
            {key: result["lamps"]["govee-left"][key] for key in ("online", "on", "brightness")},
            {"online": True, "on": True, "brightness": 51}
        )
        for forbidden in ("secret-", "private-session", "third", "last_payload"):
            self.assertNotIn(forbidden, encoded)

    def test_server_serves_only_public_assets_and_rejects_write_routes(self):
        with TemporaryDirectory(prefix="tmp_studio_") as folder:
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0), studio.handler_for(Path(folder) / "missing-token")
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            base = "http://127.0.0.1:" + str(server.server_port)
            try:
                with opener.open(base + "/") as response:
                    self.assertIn(b"Light Studio", response.read())
                for path in (
                    "/.lighting-stream.json",
                    "/server.py",
                    "/../hue-bridge/state/adapter-token",
                ):
                    with self.assertRaises(urllib.error.HTTPError) as err:
                        opener.open(base + path)
                    self.assertEqual(err.exception.code, 404)
                    err.exception.close()
                with opener.open(base + "/api/pilot") as response:
                    self.assertFalse(json.load(response)["connected"])
                request = urllib.request.Request(base + "/command", method="POST", data=b"{}")
                with self.assertRaises(urllib.error.HTTPError) as err:
                    opener.open(request)
                self.assertEqual(err.exception.code, 405)
                err.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
