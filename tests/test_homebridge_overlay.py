import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

MODULE = Path(__file__).resolve().parents[1] / 'experiments/homebridge-power/prepare_overlay.py'
spec = importlib.util.spec_from_file_location('power_overlay', MODULE)
overlay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(overlay)


class OverlayTests(unittest.TestCase):
    def fixture(self, folder):
        source = Path(folder) / 'original'
        (source / 'lib/device').mkdir(parents=True)
        (source / 'package.json').write_text(json.dumps({
            'name': '@homebridge-plugins/homebridge-govee', 'version': '11.39.1'}))
        (source / 'LICENSE').write_text('Fixture license')
        (source / 'lib/platform.js').write_text('Unchanged accessory factory')
        (source / 'lib/device/light.js').write_text(overlay.ORIGINAL_SETTER)
        return source

    def test_unknown_version_or_modified_source_produces_no_overlay(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder)
            output = Path(folder) / 'overlay'
            with self.assertRaises(ValueError):
                overlay.prepare(source, output)
            self.assertFalse(output.exists())
            (source / 'package.json').write_text('{"name":"different", "version":"11.39.1"}')
            with self.assertRaises(ValueError):
                overlay.prepare(source, output)
            self.assertFalse(output.exists())

    def test_overlay_only_replaces_setter_and_adds_two_modules(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder)
            before = (source / 'lib/device/light.js').read_bytes()
            output = Path(folder) / 'overlay'
            manifest = overlay.prepare(
                source, output, expected_hash=hashlib.sha256(before).hexdigest())
            self.assertEqual(set(manifest['files']), {
                'lib/device/light.js', 'lib/smart-lighting-power/owner-client.mjs',
                'lib/smart-lighting-power/hook.mjs'})
            self.assertEqual((source / 'lib/device/light.js').read_bytes(), before)
            self.assertEqual(
                (source / 'lib/platform.js').read_text(), 'Unchanged accessory factory')
            self.assertFalse((output / 'lib/platform.js').exists())
            self.assertTrue((output / 'LICENSE.upstream').is_file())

    def test_existing_output_is_never_overwritten(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder)
            output = Path(folder) / 'overlay'
            output.mkdir()
            (output / 'peer.txt').write_text('preserve')
            with self.assertRaises(ValueError):
                overlay.prepare(source, output)
            self.assertEqual((output / 'peer.txt').read_text(), 'preserve')
