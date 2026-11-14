import * as fs from 'node:fs';
import { createHash, randomUUID } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PowerOwnerClient } from './owner-client.mjs';

const FILES = ['lib/device/light.js', 'lib/smart-lighting-power/hook.mjs',
  'lib/smart-lighting-power/owner-client.mjs'];
const digest = value => createHash('sha256').update(value).digest('hex');

function readJson(io, file, label) {
  try { return JSON.parse(io.readFileSync(file, 'utf8')); }
  catch { throw Error(`Invalid ${label}`); }
}

export function applyOverlay(options, io = fs) {
  if (!options.artifact || !options.plugin || !options.storage) throw Error('Artifact, plugin and storage required');
  const artifact = path.resolve(options.artifact);
  const plugin = path.resolve(options.plugin);
  const storage = path.resolve(options.storage);
  const manifest = readJson(io, path.join(artifact, 'manifest.json'), 'overlay manifest');
  const pkg = readJson(io, path.join(plugin, 'package.json'), 'plugin metadata');
  if (manifest.plugin !== '@homebridge-plugins/homebridge-govee' || manifest.version !== '11.39.1'
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
  const configFile = path.join(storage, 'smart-lighting-power.json');
  const light = path.join(plugin, FILES[0]);
  const factory = path.join(plugin, 'lib/platform.js');
  if (io.lstatSync(light).isSymbolicLink()) throw Error('Symbolic light source is unsupported');
  const original = io.readFileSync(light);
  const factoryHash = digest(io.readFileSync(factory));
  if (factoryHash !== manifest.unchanged_platform_sha256) throw Error('Accessory factory changed');
  if (digest(original) === manifest.files[FILES[0]]) {
    const modulesMatch = FILES.slice(1).every(name => io.existsSync(path.join(plugin, name))
      && digest(io.readFileSync(path.join(plugin, name))) === manifest.files[name]);
    const configMatches = io.existsSync(configFile)
      && JSON.stringify(readJson(io, configFile, 'installed owner config')) === JSON.stringify(config);
    if (!modulesMatch || !configMatches) throw Error('Existing overlay differs; manual inspection required');
    return { ready: true, installed: true, alreadyInstalled: true, version: pkg.version };
  }
  if (digest(original) !== manifest.original_light_sha256) throw Error('Plugin light source changed');
  const moduleDir = path.join(plugin, 'lib/smart-lighting-power');
  if (io.existsSync(moduleDir) || io.existsSync(configFile)) throw Error('Existing owner files must be preserved');
  for (const folder of [path.dirname(light), path.join(plugin, 'lib'), storage]) {
    io.accessSync(folder, io.constants.W_OK);
  }
  io.accessSync(light, io.constants.R_OK | io.constants.W_OK);
  if (io.statSync(path.dirname(light)).dev !== io.statSync(storage).dev) {
    throw Error('Plugin and backup storage must share a filesystem');
  }
  if (!options.apply) return { ready: true, installed: false, version: pkg.version, files: FILES };

  const backup = io.mkdtempSync(path.join(storage, 'smart-lighting-power-backup-'));
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
    io.writeFileSync(configTmp, configBytes, { mode: 0o600, flag: 'wx' });
    io.linkSync(configTmp, configFile); // Never overwrite a concurrently created config.
    configCreated = true;
    io.writeFileSync(lightTmp, bytes.get(FILES[0]), { mode, flag: 'wx' });
    // Claim the actual inode first; never overwrite a peer file with rename.
    io.renameSync(light, lightClaim);
    lightClaimed = true;
    if (digest(io.readFileSync(lightClaim)) !== manifest.original_light_sha256) throw Error('Source changed during install');
    io.linkSync(lightTmp, light);
    lightReplaced = true;
    if (digest(io.readFileSync(factory)) !== factoryHash) throw Error('Factory changed during install');
    if (digest(io.readFileSync(light)) !== manifest.files[FILES[0]]) throw Error('Entry changed during install');
    return { ready: true, installed: true, alreadyInstalled: false, backup,
      version: pkg.version, factoryUnchanged: true, files: FILES };
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
      else if (['--artifact', '--plugin', '--storage'].includes(key)) options[key.slice(2)] = process.argv[++i];
      else throw Error('Unknown installer option');
    }
    console.log(JSON.stringify(applyOverlay(options)));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
