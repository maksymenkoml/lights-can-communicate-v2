import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import * as fs from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { applyOverlay } from '../experiments/homebridge-power/apply-overlay.mjs';

const digest = value => createHash('sha256').update(value).digest('hex');
function fixture() {
  const root = fs.mkdtempSync(path.join(tmpdir(), 'tmp_overlay_apply_'));
  const artifact = path.join(root, 'artifact');
  const plugin = path.join(root, 'plugin');
  const storage = path.join(root, 'storage');
  fs.mkdirSync(path.join(plugin, 'lib/device'), { recursive: true });
  fs.mkdirSync(storage);
  fs.writeFileSync(path.join(plugin, 'package.json'), JSON.stringify({
    name: '@homebridge-plugins/homebridge-govee', version: '11.39.1',
  }));
  fs.writeFileSync(path.join(plugin, 'lib/device/light.js'), 'original light');
  fs.writeFileSync(path.join(plugin, 'lib/platform.js'), 'unchanged factory');
  fs.writeFileSync(path.join(storage, 'cachedAccessories'), 'preserve all HomeKit IDs');
  fs.writeFileSync(path.join(storage, 'config.json'), 'preserve original Homebridge config');
  const files = {
    'lib/device/light.js': 'patched setter',
    'lib/smart-lighting-power/hook.mjs': 'optional wrapper',
    'lib/smart-lighting-power/owner-client.mjs': 'owner client',
  };
  for (const [name, value] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(artifact, name)), { recursive: true });
    fs.writeFileSync(path.join(artifact, name), value);
  }
  fs.writeFileSync(path.join(artifact, 'manifest.json'), JSON.stringify({
    plugin: '@homebridge-plugins/homebridge-govee', version: '11.39.1',
    original_light_sha256: digest('original light'),
    unchanged_platform_sha256: digest('unchanged factory'),
    files: Object.fromEntries(Object.entries(files).map(([name, value]) => [name, digest(value)])),
  }));
  fs.writeFileSync(path.join(artifact, 'power-config.json'), JSON.stringify({
    version: 1, endpoint: 'http://127.0.0.1:19880/power-owner', token: 'a'.repeat(48),
  }));
  return { root, artifact, plugin, storage };
}

test('preflight is read-only and rejects changed source without writes', () => {
  const f = fixture();
  try {
    assert.equal(applyOverlay(f).ready, true);
    assert.deepEqual(fs.readdirSync(f.storage).sort(), ['cachedAccessories', 'config.json']);
    fs.writeFileSync(path.join(f.plugin, 'lib/device/light.js'), 'peer change');
    assert.throws(() => applyOverlay({ ...f, apply: true }), /source|changed/i);
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/device/light.js'), 'utf8'), 'peer change');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('apply preserves factory, package and HomeKit state and creates private backup/config', () => {
  const f = fixture();
  try {
    const packageBefore = fs.readFileSync(path.join(f.plugin, 'package.json'), 'utf8');
    const result = applyOverlay({ ...f, apply: true });
    assert.equal(result.installed, true);
    assert.equal(fs.readFileSync(path.join(result.backup, 'light.js'), 'utf8'), 'original light');
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/platform.js'), 'utf8'), 'unchanged factory');
    assert.equal(fs.readFileSync(path.join(f.plugin, 'package.json'), 'utf8'), packageBefore);
    assert.equal(fs.readFileSync(path.join(f.storage, 'cachedAccessories'), 'utf8'), 'preserve all HomeKit IDs');
    assert.equal(fs.readFileSync(path.join(f.storage, 'config.json'), 'utf8'), 'preserve original Homebridge config');
    assert.equal(fs.statSync(path.join(f.storage, 'smart-lighting-power.json')).mode & 0o777, 0o600);
    assert.equal(applyOverlay({ ...f, apply: true }).alreadyInstalled, true);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('entry replacement failure rolls back only newly created files', () => {
  const f = fixture();
  try {
    const io = { ...fs, renameSync: (from, to) => {
      if (to === path.join(f.plugin, 'lib/device/light.js')) throw Object.assign(Error('fixture IO'), { code: 'EIO' });
      return fs.renameSync(from, to);
    }, linkSync: (from, to) => {
      if (to === path.join(f.plugin, 'lib/device/light.js') && path.basename(from).startsWith('tmp_smart_light-')) {
        throw Object.assign(Error('fixture IO'), { code: 'EIO' });
      }
      return fs.linkSync(from, to);
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /failed/i);
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/device/light.js'), 'utf8'), 'original light');
    assert.equal(fs.existsSync(path.join(f.plugin, 'lib/smart-lighting-power')), false);
    assert.equal(fs.existsSync(path.join(f.storage, 'smart-lighting-power.json')), false);
    assert.equal(fs.readdirSync(path.join(f.plugin, 'lib/device')).length, 1);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('peer source edit during staging is preserved rather than overwritten', () => {
  const f = fixture();
  try {
    const light = path.join(f.plugin, 'lib/device/light.js');
    const io = { ...fs, writeFileSync: (file, ...args) => {
      fs.writeFileSync(file, ...args);
      if (typeof file === 'string' && path.basename(file).startsWith('tmp_smart_light-') && file.endsWith('.js')) {
        fs.writeFileSync(light, 'peer upgrade');
      }
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /failed/i);
    assert.equal(fs.readFileSync(light, 'utf8'), 'peer upgrade');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('a peer module directory created after preflight keeps its inode', () => {
  const f = fixture();
  try {
    const directory = path.join(f.plugin, 'lib/smart-lighting-power');
    let peerInode;
    const io = { ...fs, writeFileSync: (file, ...args) => {
      fs.writeFileSync(file, ...args);
      if (typeof file === 'string' && file.includes('tmp_smart_light_modules-') && path.basename(file) === 'owner-client.mjs') {
        fs.mkdirSync(directory);
        peerInode = fs.statSync(directory).ino;
      }
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /failed/i);
    assert.equal(fs.statSync(directory).ino, peerInode);
    assert.deepEqual(fs.readdirSync(directory), []);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('rollback never unlinks a concurrently replaced private config', () => {
  const f = fixture();
  try {
    const light = path.join(f.plugin, 'lib/device/light.js');
    const config = path.join(f.storage, 'smart-lighting-power.json');
    let rollback = false;
    let injected = false;
    const io = { ...fs, linkSync: (from, to) => {
      if (to === light && path.basename(from).startsWith('tmp_smart_light-')) {
        rollback = true;
        throw Error('fixture publish failure');
      }
      return fs.linkSync(from, to);
    }, readFileSync: (file, ...args) => {
      const data = fs.readFileSync(file, ...args);
      if (rollback && !injected && path.basename(file).startsWith('rollback-config-')) {
        injected = true;
        fs.writeFileSync(config, 'peer private config');
      }
      return data;
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /requires inspection/i);
    assert.equal(injected, true);
    assert.equal(fs.readFileSync(config, 'utf8'), 'peer private config');
    assert.equal(fs.readFileSync(light, 'utf8'), 'original light');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('inherited NAS permissions are restricted before publishing the config', () => {
  const f = fixture();
  try {
    const config = path.join(f.storage, 'smart-lighting-power.json');
    const io = { ...fs, openSync: (file, ...args) => {
      const fd = fs.openSync(file, ...args);
      if (typeof file === 'string' && path.basename(file).startsWith('tmp_smart_light_config-')) fs.fchmodSync(fd, 0o777);
      return fd;
    }, writeFileSync: (file, ...args) => {
      if (typeof file === 'number') assert.equal(fs.fstatSync(file).mode & 0o777, 0o600);
      fs.writeFileSync(file, ...args);
      if (typeof file === 'string' && path.basename(file).startsWith('tmp_smart_light_config-')) fs.chmodSync(file, 0o777);
    }, linkSync: (from, to) => {
      if (to === config) assert.equal(fs.statSync(from).mode & 0o777, 0o600);
      return fs.linkSync(from, to);
    } };
    assert.equal(applyOverlay({ ...f, apply: true }, io).installed, true);
    assert.equal(fs.statSync(config).mode & 0o777, 0o600);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('a filesystem that ignores private permissions cannot activate the overlay', () => {
  const f = fixture();
  try {
    const io = { ...fs, openSync: (file, ...args) => {
      const fd = fs.openSync(file, ...args);
      if (typeof file === 'string' && path.basename(file).startsWith('tmp_smart_light_config-')) fs.fchmodSync(fd, 0o777);
      return fd;
    }, writeFileSync: (file, ...args) => {
      fs.writeFileSync(file, ...args);
      if (typeof file === 'string' && path.basename(file).startsWith('tmp_smart_light_config-')) fs.chmodSync(file, 0o777);
    }, fchmodSync: () => {} };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /failed/i);
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/device/light.js'), 'utf8'), 'original light');
    assert.equal(fs.existsSync(path.join(f.storage, 'smart-lighting-power.json')), false);
    assert.equal(fs.existsSync(path.join(f.plugin, 'lib/smart-lighting-power')), false);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('an installed overlay with a public config is rejected without changing it', () => {
  const f = fixture();
  try {
    applyOverlay({ ...f, apply: true });
    const config = path.join(f.storage, 'smart-lighting-power.json');
    fs.chmodSync(config, 0o777);
    assert.throws(() => applyOverlay({ ...f, apply: true }), /permission/i);
    assert.equal(fs.statSync(config).mode & 0o777, 0o777);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

function upgradeFixture() {
  const f = fixture();
  applyOverlay({ ...f, apply: true });
  f.previousManifest = path.join(f.root, 'previous-manifest.json');
  fs.copyFileSync(path.join(f.artifact, 'manifest.json'), f.previousManifest);
  const manifest = JSON.parse(fs.readFileSync(f.previousManifest, 'utf8'));
  for (const name of ['hook.mjs', 'owner-client.mjs']) {
    const relative = 'lib/smart-lighting-power/' + name;
    fs.writeFileSync(path.join(f.artifact, relative), 'updated ' + name);
    manifest.files[relative] = digest('updated ' + name);
  }
  fs.writeFileSync(path.join(f.artifact, 'manifest.json'), JSON.stringify(manifest));
  return f;
}

test('exact previous manifest permits a two-module upgrade without touching identity or credentials', () => {
  const f = upgradeFixture();
  try {
    const light = path.join(f.plugin, 'lib/device/light.js');
    const config = path.join(f.storage, 'smart-lighting-power.json');
    const lightInode = fs.statSync(light).ino;
    const configInode = fs.statSync(config).ino;
    const before = fs.readdirSync(f.storage).sort();
    assert.equal(applyOverlay(f).upgradeReady, true);
    assert.deepEqual(fs.readdirSync(f.storage).sort(), before);
    const result = applyOverlay({ ...f, apply: true });
    assert.equal(result.upgraded, true);
    assert.equal(fs.readFileSync(path.join(result.backup, 'hook.mjs'), 'utf8'), 'optional wrapper');
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/smart-lighting-power/hook.mjs'), 'utf8'),
      'updated hook.mjs');
    assert.equal(fs.statSync(light).ino, lightInode);
    assert.equal(fs.statSync(config).ino, configInode);
    assert.equal(fs.readFileSync(path.join(f.storage, 'cachedAccessories'), 'utf8'), 'preserve all HomeKit IDs');
    assert.equal(applyOverlay({ ...f, apply: true }).alreadyInstalled, true);
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('module upgrade rejects an unexpected installed module before staging', () => {
  const f = upgradeFixture();
  try {
    const hook = path.join(f.plugin, 'lib/smart-lighting-power/hook.mjs');
    fs.writeFileSync(hook, 'peer module');
    const before = fs.readdirSync(f.storage).sort();
    assert.throws(() => applyOverlay({ ...f, apply: true }), /differs|changed/i);
    assert.deepEqual(fs.readdirSync(f.storage).sort(), before);
    assert.equal(fs.readFileSync(hook, 'utf8'), 'peer module');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('second module publication failure rolls both modules back', () => {
  const f = upgradeFixture();
  try {
    const io = { ...fs, linkSync: (from, to) => {
      if (path.basename(from).startsWith('tmp_smart_light_upgrade-') && to.endsWith('owner-client.mjs')) {
        throw Error('fixture publication failure');
      }
      return fs.linkSync(from, to);
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /rollback complete/i);
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/smart-lighting-power/hook.mjs'), 'utf8'),
      'optional wrapper');
    assert.equal(fs.readFileSync(path.join(f.plugin, 'lib/smart-lighting-power/owner-client.mjs'), 'utf8'),
      'owner client');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});

test('peer edit immediately before upgrade claim is restored without being overwritten', () => {
  const f = upgradeFixture();
  try {
    const hook = path.join(f.plugin, 'lib/smart-lighting-power/hook.mjs');
    let injected = false;
    const io = { ...fs, renameSync: (from, to) => {
      if (from === hook && path.basename(to) === 'hook.mjs') {
        injected = true;
        fs.writeFileSync(hook, 'peer after preflight');
      }
      return fs.renameSync(from, to);
    } };
    assert.throws(() => applyOverlay({ ...f, apply: true }, io), /failed/i);
    assert.equal(injected, true);
    assert.equal(fs.readFileSync(hook, 'utf8'), 'peer after preflight');
  } finally { fs.rmSync(f.root, { recursive: true }); }
});
