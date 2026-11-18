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
    def fixture(self, folder, version='11.39.1'):
        source = Path(folder) / 'original'
        (source / 'lib/device').mkdir(parents=True)
        (source / 'package.json').write_text(json.dumps({
            'name': '@homebridge-plugins/homebridge-govee', 'version': version}))
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
            manifest = overlay.prepare(source, output, release={
                'light': hashlib.sha256(before).hexdigest(),
                'platform': hashlib.sha256(b'Unchanged accessory factory').hexdigest()})
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

    def test_each_supported_release_pins_its_own_light_and_factory(self):
        self.assertEqual(set(overlay.SUPPORTED_RELEASES), {'11.39.1', '11.40.0'})
        self.assertEqual(overlay.SUPPORTED_RELEASES['11.40.0'], {
            'light': '6c46bc033b6c16e173c90095fc0a28ef8c1b20474c99dc4fcf6e23947dcdb831',
            'platform': '3012b6e7ebbd78142daf0177261c92aabe54dc10aa4ad36e37cef636c1bf3b2a'})
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            # A fixture with 11.40.0 metadata but different bytes is refused.
            source = self.fixture(folder, '11.40.0')
            output = Path(folder) / 'overlay'
            with self.assertRaisesRegex(ValueError, 'verified release'):
                overlay.prepare(source, output)
            self.assertFalse(output.exists())

    def test_newer_release_overlay_records_its_version_and_hashes(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder, '11.40.0')
            light = hashlib.sha256((source / 'lib/device/light.js').read_bytes()).hexdigest()
            factory = hashlib.sha256((source / 'lib/platform.js').read_bytes()).hexdigest()
            manifest = overlay.prepare(source, Path(folder) / 'overlay',
                                       release={'light': light, 'platform': factory})
            self.assertEqual(manifest['version'], '11.40.0')
            self.assertEqual(manifest['original_light_sha256'], light)
            self.assertEqual(manifest['unchanged_platform_sha256'], factory)

    def test_changed_factory_produces_no_overlay(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder, '11.40.0')
            light = hashlib.sha256((source / 'lib/device/light.js').read_bytes()).hexdigest()
            output = Path(folder) / 'overlay'
            with self.assertRaisesRegex(ValueError, 'factory'):
                overlay.prepare(source, output, release={'light': light, 'platform': '0' * 64})
            self.assertFalse(output.exists())

    def test_unverified_release_produces_no_overlay(self):
        with TemporaryDirectory(prefix='tmp_power_overlay_') as folder:
            source = self.fixture(folder, '11.41.0')
            output = Path(folder) / 'overlay'
            with self.assertRaisesRegex(ValueError, 'verified Homebridge Govee version'):
                overlay.prepare(source, output)
            self.assertFalse(output.exists())
