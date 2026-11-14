"""Prepare an exact-version, identity-preserving pilot overlay; never install it."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

VERSION = '11.39.1'
SOURCE_HASH = '1cb33db175d1e4fc2b8d0fac3a3f81453b517f314d044be6762414345056902e'
ORIGINAL_SETTER = '''    this.service.getCharacteristic(this.hapChar.On).onSet(async (value) => {
      await this.internalStateUpdate(value)
    })'''
PATCHED_SETTER = (
    "    this.service.getCharacteristic(this.hapChar.On).onSet("
    "coordinatedSetter(this, async (value) => {\n"
    "      await this.internalStateUpdate(value)\n"
    "    }))"
)


def prepare(source, output, expected_hash=SOURCE_HASH):
    source, output = Path(source), Path(output)
    if output.exists() or output.resolve().is_relative_to(source.resolve()):
        raise ValueError('Overlay output must be a new directory outside upstream source')
    package = json.loads((source / 'package.json').read_text())
    if (package.get('name') != '@homebridge-plugins/homebridge-govee'
            or package.get('version') != VERSION):
        raise ValueError('Only the verified Homebridge Govee version is supported')
    original = (source / 'lib/device/light.js').read_bytes()
    if hashlib.sha256(original).hexdigest() != expected_hash:
        raise ValueError('Upstream source differs from the verified release')
    text = original.decode()
    if text.count(ORIGINAL_SETTER) != 1:
        raise ValueError('Expected exactly one original main-light On setter')
    modified = (
        "import { coordinatedSetter } from '../smart-lighting-power/hook.mjs'\n"
        + text.replace(ORIGINAL_SETTER, PATCHED_SETTER, 1)
    )
    files = {
        'lib/device/light.js': modified.encode(),
        **{f'lib/smart-lighting-power/{name}': (Path(__file__).parent / name).read_bytes()
           for name in ('owner-client.mjs', 'hook.mjs')},
    }
    manifest = {'plugin': package['name'], 'version': VERSION,
                'original_light_sha256': expected_hash,
                'unchanged_platform_sha256': hashlib.sha256(
                    (source / 'lib/platform.js').read_bytes()).hexdigest(),
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    output.mkdir(parents=True)
    try:
        for name, data in files.items():
            destination = output / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        shutil.copyfile(source / 'LICENSE', output / 'LICENSE.upstream')
        (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    except Exception:
        shutil.rmtree(output)
        raise
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = prepare(args.source, args.output)
    print(json.dumps(result, indent=2))
