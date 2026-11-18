"""Prepare an exact-version, identity-preserving pilot overlay; never install it."""

import argparse
import hashlib
import json
import shutil
from pathlib import Path

# Public tagged releases whose light handler and accessory factory were compared
# with the coordinated setter and native writer contracts.
SUPPORTED_RELEASES = {
    '11.39.1': {
        'light': '1cb33db175d1e4fc2b8d0fac3a3f81453b517f314d044be6762414345056902e',
        'platform': 'bd33669d19765d92a80f1743e939ed0f0c67913f9e029010ebb8b4127f7003ed',
    },
    '11.40.0': {
        'light': '6c46bc033b6c16e173c90095fc0a28ef8c1b20474c99dc4fcf6e23947dcdb831',
        'platform': '3012b6e7ebbd78142daf0177261c92aabe54dc10aa4ad36e37cef636c1bf3b2a',
    },
}
ORIGINAL_SETTER = '''    this.service.getCharacteristic(this.hapChar.On).onSet(async (value) => {
      await this.internalStateUpdate(value)
    })'''
PATCHED_SETTER = (
    "    this.service.getCharacteristic(this.hapChar.On).onSet("
    "coordinatedSetter(this, async (value) => {\n"
    "      await this.internalStateUpdate(value)\n"
    "    }))"
)


def prepare(source, output, release=None):
    source, output = Path(source), Path(output)
    if output.exists() or output.resolve().is_relative_to(source.resolve()):
        raise ValueError('Overlay output must be a new directory outside upstream source')
    package = json.loads((source / 'package.json').read_text())
    version = package.get('version')
    if (package.get('name') != '@homebridge-plugins/homebridge-govee'
            or version not in SUPPORTED_RELEASES):
        raise ValueError('Only a verified Homebridge Govee version is supported')
    release = release or SUPPORTED_RELEASES[version]
    original = (source / 'lib/device/light.js').read_bytes()
    if hashlib.sha256(original).hexdigest() != release['light']:
        raise ValueError('Upstream source differs from the verified release')
    factory = hashlib.sha256((source / 'lib/platform.js').read_bytes()).hexdigest()
    if factory != release['platform']:
        raise ValueError('Upstream accessory factory differs from the verified release')
    text = original.decode()
    if text.count(ORIGINAL_SETTER) != 1:
        raise ValueError('Expected exactly one original main-light On setter')
    modified = (
        "import { coordinatedSetter } from '../lights-can-communicate-v2-power/hook.mjs'\n"
        + text.replace(ORIGINAL_SETTER, PATCHED_SETTER, 1)
    )
    files = {
        'lib/device/light.js': modified.encode(),
        **{f'lib/lights-can-communicate-v2-power/{name}':
           (Path(__file__).parent / name).read_bytes()
           for name in ('owner-client.mjs', 'hook.mjs')},
    }
    manifest = {'plugin': package['name'], 'version': version,
                'original_light_sha256': release['light'],
                'unchanged_platform_sha256': factory,
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
