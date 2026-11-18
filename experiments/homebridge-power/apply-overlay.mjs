import * as fs from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PowerOwnerClient } from './owner-client.mjs';

const FILES = ['lib/device/light.js', 'lib/lights-can-communicate-v2-power/hook.mjs',
  'lib/lights-can-communicate-v2-power/owner-client.mjs'];
// prepare_overlay.py pins each release's exact public light and factory hashes.
const SUPPORTED_VERSIONS = new Set(['11.39.1', '11.40.0']);
const digest = value => createHash('sha256').update(value).digest('hex');

function readJson(io, file, label) {
  try { return JSON.parse(io.readFileSync(file, 'utf8')); }
  catch { throw Error(`Invalid ${label}`); }
}

// A plugin upgrade replaces the package but keeps owner files in storage. Reuse
// only an identical private config; never rewrite or widen its permissions.
function existingConfig(io, file, config) {
  const stat = io.lstatSync(file, { bigint: true });
  if (!stat.isFile() || stat.isSymbolicLink() || (Number(stat.mode) & 0o777) !== 0o600) {
    throw Error('Installed owner config permissions require inspection');
  }
  if (JSON.stringify(readJson(io, file, 'installed owner config')) !== JSON.stringify(config)) {
    throw Error('Existing owner config differs; manual inspection required');
  }
  return `${stat.dev}:${stat.ino}`;
}

function upgradeModules(options, io, manifest, bytes, plugin, storage) {
  const originals = new Map();
  const previous = readJson(io, path.resolve(options.previousManifest), 'previous manifest');
  if (previous.plugin !== manifest.plugin || previous.version !== manifest.version
    || previous.unchanged_platform_sha256 !== manifest.unchanged_platform_sha256
    || previous.original_light_sha256 !== manifest.original_light_sha256
    || Object.keys(previous.files || {}).sort().join('|') !== [...FILES].sort().join('|')
    || previous.files[FILES[0]] !== manifest.files[FILES[0]]) {
    throw Error('Previous overlay manifest differs');
  }
  for (const name of FILES) {
    const file = path.join(plugin, name);
    const stat = io.lstatSync(file);
    const data = io.readFileSync(file);
    if (!stat.isFile() || stat.isSymbolicLink()
      || digest(data) !== previous.files[name]) throw Error('Installed module changed');
    originals.set(name, data);
  }
  const directory = path.join(plugin, 'lib/lights-can-communicate-v2-power');
  if (io.lstatSync(directory).isSymbolicLink()) throw Error('Symbolic module directory unsupported');
  io.accessSync(directory, io.constants.W_OK);
  io.accessSync(storage, io.constants.W_OK);
  if (io.statSync(directory).dev !== io.statSync(storage).dev) throw Error('Module backup filesystem differs');
  if (!options.apply) return { ready: true, installed: true, upgradeReady: true, files: FILES.slice(1) };

  const backup = io.mkdtempSync(path.join(storage, 'lights-can-communicate-v2-power-backup-'));
  io.chmodSync(backup, 0o700);
  if ((io.statSync(backup).mode & 0o777) !== 0o700) throw Error('Private backup mode unavailable');
  io.writeFileSync(path.join(backup, 'previous-manifest.json'), JSON.stringify(previous, null, 2),
    { flag: 'wx', mode: 0o600 });
  for (const name of FILES.slice(1)) {
    io.writeFileSync(path.join(backup, path.basename(name) + '.snapshot'), originals.get(name),
      { flag: 'wx', mode: 0o600 });
  }
  const records = FILES.slice(1).map(name => ({ name, file: path.join(plugin, name),
    temporary: path.join(directory, `tmp_smart_light_upgrade-${randomUUID()}.mjs`),
    claim: path.join(backup, path.basename(name)), claimed: false, published: false }));
  try {
    for (const record of records) {
      const fd = io.openSync(record.temporary, 'wx', 0o644);
      try {
        io.fchmodSync(fd, 0o644);
        if ((io.fstatSync(fd).mode & 0o777) !== 0o644) throw Error('Runtime module mode unavailable');
        io.writeFileSync(fd, bytes.get(record.name));
      } finally { io.closeSync(fd); }
    }
    for (const record of records) {
      io.renameSync(record.file, record.claim);
      record.claimed = true;
      if (digest(io.readFileSync(record.claim)) !== previous.files[record.name]) {
        throw Error('Module changed while claiming upgrade');
      }
      io.linkSync(record.temporary, record.file);
      record.published = true;
    }
    if (digest(io.readFileSync(path.join(plugin, 'lib/platform.js')))
      !== manifest.unchanged_platform_sha256) throw Error('Factory changed during upgrade');
    for (const name of FILES) {
      if (digest(io.readFileSync(path.join(plugin, name))) !== manifest.files[name]) {
        throw Error('Published overlay changed');
      }
    }
    return { ready: true, installed: true, upgraded: true, backup, factoryUnchanged: true,
      version: manifest.version, files: FILES.slice(1) };
  } catch {
    let restored = true;
    for (const record of [...records].reverse()) {
      if (!record.claimed) continue;
      try {
        if (record.published) {
          const currentClaim = path.join(backup, `rollback-${randomUUID()}-${path.basename(record.name)}`);
          io.renameSync(record.file, currentClaim);
          if (digest(io.readFileSync(currentClaim)) !== manifest.files[record.name]) {
            io.linkSync(currentClaim, record.file);
            throw Error('Peer module preserved');
          }
        }
        io.linkSync(record.claim, record.file);
      } catch { restored = false; }
    }
    throw Error(`Overlay upgrade failed; rollback ${restored ? 'complete' : 'requires inspection'}; backup ${backup}`);
  } finally {
    for (const record of records) io.rmSync(record.temporary, { force: true });
  }
}

export function applyOverlay(options, io = fs) {
  if (!options.artifact || !options.plugin || !options.storage) throw Error('Artifact, plugin and storage required');
  const artifact = path.resolve(options.artifact);
  const plugin = path.resolve(options.plugin);
  const storage = path.resolve(options.storage);
  const manifest = readJson(io, path.join(artifact, 'manifest.json'), 'overlay manifest');
  const pkg = readJson(io, path.join(plugin, 'package.json'), 'plugin metadata');
  if (manifest.plugin !== '@homebridge-plugins/homebridge-govee' || !SUPPORTED_VERSIONS.has(manifest.version)
    || pkg.name !== manifest.plugin || pkg.version !== manifest.version) throw Error('Unsupported plugin version');
  if (!manifest.files || Object.keys(manifest.files).sort().join('|') !== [...FILES].sort().join('|')) {
    throw Error('Unexpected overlay file set');
  }
  const bytes = new Map();
  for (const name of FILES) {
    const source = path.join(artifact, name);
    if (!io.lstatSync(source).isFile() || io.lstatSync(source).isSymbolicLink()) throw Error('Invalid artifact member');
    const data = io.readFileSync(source);
    if (digest(data) !== manifest.files[name]) throw Error('Artifact checksum differs');
    bytes.set(name, data);
  }
  const config = readJson(io, path.join(artifact, 'power-config.json'), 'private power configuration');
  if (config?.version !== 1 || Object.keys(config).some(key => !['version', 'endpoint', 'token'].includes(key))) {
    throw Error('Invalid private power configuration');
  }
  // Constructor validates endpoint/credential without opening a connection.
  new PowerOwnerClient({ endpoint: config.endpoint, token: config.token });
  const configBytes = Buffer.from(JSON.stringify(config, null, 2) + '\n');
  const configFile = path.join(storage, 'lights-can-communicate-v2-power.json');
  const light = path.join(plugin, FILES[0]);
  const factory = path.join(plugin, 'lib/platform.js');
  if (io.lstatSync(light).isSymbolicLink()) throw Error('Symbolic light source is unsupported');
  const original = io.readFileSync(light);
  const factoryHash = digest(io.readFileSync(factory));
  if (factoryHash !== manifest.unchanged_platform_sha256) throw Error('Accessory factory changed');
  if (digest(original) === manifest.files[FILES[0]]) {
    const modulesMatch = FILES.slice(1).every(name => io.existsSync(path.join(plugin, name))
      && digest(io.readFileSync(path.join(plugin, name))) === manifest.files[name]);
    if (!io.existsSync(configFile)) throw Error('Existing overlay differs; manual inspection required');
    existingConfig(io, configFile, config);
    if (!modulesMatch) {
      if (!options.previousManifest) throw Error('Existing overlay differs; manual inspection required');
      return upgradeModules(options, io, manifest, bytes, plugin, storage);
    }
    return { ready: true, installed: true, alreadyInstalled: true, version: pkg.version };
  }
  if (digest(original) !== manifest.original_light_sha256) throw Error('Plugin light source changed');
  const moduleDir = path.join(plugin, 'lib/lights-can-communicate-v2-power');
  if (io.existsSync(moduleDir)) throw Error('Existing owner files must be preserved');
  const reusedConfig = io.existsSync(configFile) ? existingConfig(io, configFile, config) : undefined;
  const configReused = reusedConfig !== undefined;
  for (const folder of [path.dirname(light), path.join(plugin, 'lib'), storage]) {
    io.accessSync(folder, io.constants.W_OK);
  }
  io.accessSync(light, io.constants.R_OK | io.constants.W_OK);
  if (io.statSync(path.dirname(light)).dev !== io.statSync(storage).dev) {
    throw Error('Plugin and backup storage must share a filesystem');
  }
  if (!options.apply) return { ready: true, installed: false, version: pkg.version, files: FILES, configReused };

  const backup = io.mkdtempSync(path.join(storage, 'lights-can-communicate-v2-power-backup-'));
  io.chmodSync(backup, 0o700);
  io.writeFileSync(path.join(backup, 'light.js'), original, { mode: 0o600, flag: 'wx' });
  io.writeFileSync(path.join(backup, 'manifest.json'), JSON.stringify(manifest, null, 2), { mode: 0o600 });
  const moduleTmp = path.join(plugin, `lib/tmp_smart_light_modules-${randomUUID()}`);
  const configTmp = path.join(storage, `tmp_smart_light_config-${randomUUID()}`);
  const lightTmp = path.join(path.dirname(light), `tmp_smart_light-${randomUUID()}.js`);
  const lightClaim = path.join(backup, 'claimed-light.js');
  const mode = io.statSync(light).mode & 0o777;
  let modulesCreated = false;
  let moduleIdentity;
  let configCreated = false;
  let lightClaimed = false;
  let lightReplaced = false;
  try {
    io.mkdirSync(moduleTmp, { mode: 0o755 });
    for (const name of FILES.slice(1)) {
      io.writeFileSync(path.join(moduleTmp, path.basename(name)), bytes.get(name), { mode: 0o644, flag: 'wx' });
    }
    // Exclusive creation cannot replace even an empty concurrently created directory.
    io.mkdirSync(moduleDir, { mode: 0o755 });
    modulesCreated = true;
    const created = io.statSync(moduleDir, { bigint: true });
    moduleIdentity = `${created.dev}:${created.ino}`;
    for (const name of FILES.slice(1)) {
      io.linkSync(path.join(moduleTmp, path.basename(name)), path.join(plugin, name));
    }
    if (!configReused) {
      const configFd = io.openSync(configTmp, 'wx', 0o600);
      try {
        // NAS ACL inheritance can override the creation mode. Restrict and verify
        // the actual inode before writing any credential bytes or publishing it.
        io.fchmodSync(configFd, 0o600);
        if ((io.fstatSync(configFd).mode & 0o777) !== 0o600) throw Error('Private config permissions unavailable');
        io.writeFileSync(configFd, configBytes);
      } finally { io.closeSync(configFd); }
      io.linkSync(configTmp, configFile); // Never overwrite a concurrently created config.
      configCreated = true;
    }
    io.writeFileSync(lightTmp, bytes.get(FILES[0]), { mode, flag: 'wx' });
    // Claim the actual inode first; never overwrite a peer file with rename.
    io.renameSync(light, lightClaim);
    lightClaimed = true;
    if (digest(io.readFileSync(lightClaim)) !== manifest.original_light_sha256) throw Error('Source changed during install');
    io.linkSync(lightTmp, light);
    lightReplaced = true;
    if (digest(io.readFileSync(factory)) !== factoryHash) throw Error('Factory changed during install');
    if (digest(io.readFileSync(light)) !== manifest.files[FILES[0]]) throw Error('Entry changed during install');
    if (configReused && existingConfig(io, configFile, config) !== reusedConfig) {
      throw Error('Owner config changed during install');
    }
    return { ready: true, installed: true, alreadyInstalled: false, backup,
      version: pkg.version, factoryUnchanged: true, files: FILES, configReused };
  } catch {
    let restored = true;
    try {
      if (lightReplaced) {
        const currentClaim = path.join(backup, `rollback-light-${randomUUID()}.js`);
        io.renameSync(light, currentClaim);
        if (digest(io.readFileSync(currentClaim)) !== manifest.files[FILES[0]]) {
          io.linkSync(currentClaim, light);
          throw Error('Peer light preserved');
        }
        io.linkSync(lightClaim, light);
      } else if (lightClaimed) {
        io.linkSync(lightClaim, light);
        if (digest(io.readFileSync(lightClaim)) !== manifest.original_light_sha256) {
          throw Error('Peer source restored; dependent files retained for inspection');
        }
      }
      if (configCreated) {
        const configClaim = path.join(backup, `rollback-config-${randomUUID()}.json`);
        io.renameSync(configFile, configClaim);
        if (digest(io.readFileSync(configClaim)) !== digest(configBytes)) {
          io.linkSync(configClaim, configFile);
          throw Error('Peer config preserved');
        }
        if (io.existsSync(configFile)) throw Error('Peer created a replacement config');
      }
      if (modulesCreated) {
        const moduleClaim = path.join(backup, `rollback-modules-${randomUUID()}`);
        io.renameSync(moduleDir, moduleClaim);
        const claimed = io.statSync(moduleClaim, { bigint: true });
        if (`${claimed.dev}:${claimed.ino}` !== moduleIdentity) throw Error('Peer module directory retained in backup');
        const names = io.readdirSync(moduleClaim).sort();
        if (names.join('|') !== ['hook.mjs', 'owner-client.mjs'].join('|')) throw Error('Peer changed modules');
        for (const name of FILES.slice(1)) {
          if (digest(io.readFileSync(path.join(moduleClaim, path.basename(name)))) !== manifest.files[name]) throw Error('Peer changed module');
        }
        if (io.existsSync(moduleDir)) throw Error('Peer created replacement modules');
      }
    } catch { restored = false; }
    throw Error(`Overlay install failed; rollback ${restored ? 'complete' : 'requires inspection'}; backup ${backup}`);
  } finally {
    io.rmSync(configTmp, { force: true });
    io.rmSync(lightTmp, { force: true });
    io.rmSync(moduleTmp, { recursive: true, force: true });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = {};
    for (let i = 2; i < process.argv.length; i++) {
      const key = process.argv[i];
      if (key === '--apply') options.apply = true;
      else if (key === '--previous-manifest') options.previousManifest = process.argv[++i];
      else if (['--artifact', '--plugin', '--storage'].includes(key)) options[key.slice(2)] = process.argv[++i];
      else throw Error('Unknown installer option');
    }
    console.log(JSON.stringify(applyOverlay(options)));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
