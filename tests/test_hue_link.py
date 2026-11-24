"""The verified connection to the pinned Hue bridge and its address (#60 stage 3)."""

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bridge"))
from hue_link import CertificateChanged, HueBridgeLink  # noqa: E402

PINNED = "ecb5fafffea26d51"
OTHER = "0017880000000000"


class Connection:
    def __init__(self, address):
        self.address, self.closed = address, False

    def close(self):
        self.closed = True


class Network:
    """Addresses -> the bridge ID a verified connection there presents, or an error."""

    def __init__(self, devices):
        self.devices, self.opened = dict(devices), []

    def __call__(self, address, timeout):
        self.opened.append(address)
        device = self.devices.get(address)
        if device is None:
            raise OSError("unreachable")
        if isinstance(device, Exception):
            raise device
        connection = Connection(address)
        return connection, device


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class HueBridgeLinkTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="tmp_hue_link_")
        self.addCleanup(temp.cleanup)
        self.pin = Path(temp.name) / "hue-bridge-id"
        self.clock = Clock()

    def link(self, network, found=(), pin=PINNED):
        if pin is not None:
            self.pin.write_text(pin + "\n")
        return HueBridgeLink("192.168.10.21", self.pin, find=lambda: list(found),
                             clock=self.clock, connector=network)

    def test_a_verified_connection_to_the_pinned_bridge(self):
        network = Network({"192.168.10.21": PINNED})
        connection = self.link(network).connect()
        self.assertEqual((connection.address, connection.closed), ("192.168.10.21", False))

    def test_the_first_verified_connection_records_the_pin(self):
        link = self.link(Network({"192.168.10.21": PINNED}), pin=None)
        link.connect()
        self.assertEqual(self.pin.read_text(), PINNED + "\n")

    def test_another_bridge_at_the_address_never_gets_a_connection(self):
        network = Network({"192.168.10.21": OTHER})
        with self.assertRaises(CertificateChanged):
            self.link(network).connect()

    def test_the_link_follows_the_pinned_bridge_to_a_new_address(self):
        # The old address now holds another bridge; the pinned one moved.
        network = Network({"192.168.10.21": OTHER, "192.168.10.30": OTHER,
                           "192.168.10.44": PINNED})
        link = self.link(network, found=["192.168.10.21", "192.168.10.30", "192.168.10.44"])
        connection = link.connect()
        self.assertEqual((connection.address, link.address), ("192.168.10.44", "192.168.10.44"))
        # The address that failed is not asked again during the search.
        self.assertEqual(network.opened.count("192.168.10.21"), 1)

    def test_an_unreachable_bridge_is_searched_at_most_every_30_seconds(self):
        searches = []
        network = Network({})
        link = HueBridgeLink("192.168.10.21", self.pin, clock=self.clock, connector=network,
                             find=lambda: searches.append(1) or [])
        self.pin.write_text(PINNED + "\n")
        for _ in range(3):
            with self.assertRaises(OSError):
                link.connect()
        self.assertEqual(len(searches), 1)
        self.clock.now += 30
        with self.assertRaises(OSError):
            link.connect()
        self.assertEqual(len(searches), 2)

    def test_without_a_pin_or_a_search_the_link_never_moves(self):
        network = Network({"192.168.10.44": PINNED})
        link = self.link(network, found=["192.168.10.44"], pin=None)
        with self.assertRaises(OSError):
            link.connect()
        self.assertEqual(link.address, "192.168.10.21")
        unsearched = HueBridgeLink("192.168.10.21", self.pin, connector=network)
        with self.assertRaises(OSError):
            unsearched.connect()

    def test_a_string_pin_path_still_follows_the_bridge(self):
        self.pin.write_text(PINNED + "\n")
        network = Network({"192.168.10.44": PINNED})
        link = HueBridgeLink("192.168.10.21", str(self.pin), find=lambda: ["192.168.10.44"],
                             clock=self.clock, connector=network)
        self.assertEqual(link.connect().address, "192.168.10.44")

    def test_candidates_outside_the_local_network_are_skipped(self):
        network = Network({"8.8.8.8": PINNED})
        link = self.link(network, found=["8.8.8.8", "not an address"])
        with self.assertRaises(OSError):
            link.connect()
        self.assertNotIn("8.8.8.8", network.opened)

    def test_a_client_that_failed_on_the_old_address_uses_the_new_one(self):
        network = Network({"192.168.10.44": PINNED})
        link = self.link(network, found=[])
        link.address = "192.168.10.44"  # another client already followed the bridge
        self.assertEqual(link.relocate("192.168.10.21"), "192.168.10.44")


if __name__ == "__main__":
    unittest.main()
