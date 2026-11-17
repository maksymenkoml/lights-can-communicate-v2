// Check the pinned actual SDK debounce and native-write ordering without devices.
import assert from 'node:assert/strict';
import { AsyncResource } from 'node:async_hooks';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { coordinatedNativeWrites } from '../experiments/homebridge-power/hook.mjs';

const source = readFileSync(process.argv[2], 'utf8');
// Public light.js of each release accepted by prepare_overlay.py.
const VERIFIED_LIGHT = {
  '1cb33db175d1e4fc2b8d0fac3a3f81453b517f314d044be6762414345056902e': '11.39.1',
  '6c46bc033b6c16e173c90095fc0a28ef8c1b20474c99dc4fcf6e23947dcdb831': '11.40.0',
};
assert.ok(VERIFIED_LIGHT[createHash('sha256').update(source).digest('hex')],
  'Light source is not a verified Homebridge Govee release');
const method = source.slice(source.indexOf('  async internalBrightnessUpdate('),
  source.indexOf('  async internalColourUpdate('));
// 11.40.0 writers call this helper; it only polls AWS for raw-zone models.
const refreshStart = source.indexOf('  refreshZonesSoon() {');
const helpers = refreshStart < 0 ? '' : source.slice(refreshStart, source.indexOf('  externalUpdate(params) {'));
const RIGHT = 'D4:A5:D2:21:C0:46:4F:55';

function fixture(sleep, send) {
  let key = 0;
  const Native = new Function('sleep', 'generateRandomString', 'platformLang', 'parseError',
    'return class NativeBrightness {' + method + helpers + '}')(sleep, () => String(++key), {}, String);
  const native = { on: true, brightness: 51 };
  const writes = [];
  const control = new Native();
  Object.assign(control, {
    cacheState: 'on', cacheBright: 51,
    accessory: { context: { gvDeviceId: RIGHT }, log() {} },
    hapChar: { On: 'on', Brightness: 'brightness' },
    service: { getCharacteristic: () => ({ value: native.on }), updateCharacteristic() {} },
    platform: { sendDeviceUpdate: async (_accessory, params) => {
      writes.push(params);
      if (params.cmd === 'brightness') { native.brightness = params.value; native.on = true; }
      await send?.(params);
    } },
    failUpdate(error) { throw error; },
  });
  const client = { desired: new Map([['right', true]]), confirmed: () => true,
    power: async (_id, value, operation) => operation() };
  const power = coordinatedNativeWrites(control, RIGHT, client, async value => {
    native.on = value;
    writes.push({ cmd: 'state', value: value ? 'on' : 'off' });
  });
  return { control, native, power, writes };
}

test('actual SDK pending brightness is cancelled by Off before the native sender', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const f = fixture(async () => { entered(); await held; });
  const brightness = f.control.internalBrightnessUpdate(80);
  await started;
  const off = f.power(false);
  release();
  await Promise.all([brightness, off]);
  assert.equal(f.native.on, false);
  assert.equal(f.native.brightness, 51);
  assert.deepEqual(f.writes, [{ cmd: 'state', value: 'off' }]);
});

test('actual SDK in-flight brightness completes before physical Off is sent', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const f = fixture(async () => {}, async () => { entered(); await held; });
  const brightness = f.control.internalBrightnessUpdate(80);
  await started;
  const off = f.power(false);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.writes, [{ cmd: 'brightness', value: 80 }]);
  release();
  await Promise.all([brightness, off]);
  assert.equal(f.native.on, false);
  assert.deepEqual(f.writes, [{ cmd: 'brightness', value: 80 }, { cmd: 'state', value: 'off' }]);
});

test('actual SDK zero brightness sends exactly one coordinated Off', async () => {
  // 11.40.0 turns Home 0% into a native state Off; the owner must send it once.
  const f = fixture(async () => {});
  await f.control.internalBrightnessUpdate(0);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.native.on, false);
  assert.deepEqual(f.writes, [{ cmd: 'state', value: 'off' }]);
});

const ctMethod = source.slice(source.indexOf('  async internalCTUpdate('),
  source.indexOf('  async internalSceneUpdate('));
test('actual SDK delayed CT callback cannot revive a Home tile after offline Off', async () => {
  const callbacks = [];
  const Native = new Function('sleep', 'generateRandomString', 'platformLang', 'parseError',
    'm2hs', 'k2rgb', 'setTimeout', 'return class NativeCT {' + ctMethod + helpers + '}')(
    async () => {}, () => 'key', {}, String, () => [1, 2], () => [3, 4, 5],
    callback => { callbacks.push(AsyncResource.bind(callback)); });
  const values = new Map();
  const control = new Native();
  Object.assign(control, {
    cacheState: 'on', minKelvin: 2000, maxKelvin: 9000, hasScenes: true, usedCodes: [],
    accessory: { context: { gvDeviceId: RIGHT }, log() {} },
    hapChar: { On: 'on', Hue: 'hue', Saturation: 'sat' }, cusChar: { ColourMode: 'mode' },
    service: { updateCharacteristic(name, value) { values.set(name, value); } },
    platform: { sendDeviceUpdate: async () => {} },
    failUpdate(error) { throw error; },
  });
  const client = { desired: new Map([['right', true]]), confirmed: () => true,
    power: async (_id, _value, operation) => operation() };
  const power = coordinatedNativeWrites(control, RIGHT, client, async value => {
    control.cacheState = value ? 'on' : 'off';
  });
  await control.internalCTUpdate(200);
  assert.equal(callbacks.length, 1);
  await power(false);
  // A real timer retains its creation context; emulate it with AsyncResource.
  callbacks[0]();
  assert.equal(values.get('on'), false);
});
