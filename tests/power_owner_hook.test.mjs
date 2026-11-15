import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { coordinatedNativeWrites, coordinatedSetter } from '../experiments/homebridge-power/hook.mjs';

const RIGHT = 'D4:A5:D2:21:C0:46:4F:55';

function control(folder, id, on = true) {
  const shutdown = [];
  const warnings = [];
  return {
    accessory: { context: { gvDeviceId: id }, logWarn: message => warnings.push(message) },
    service: { getCharacteristic: () => ({ value: on }) }, hapChar: { On: 'on' },
    platform: { api: { user: { storagePath: () => folder },
      on: (event, callback) => { assert.equal(event, 'shutdown'); shutdown.push(callback); } } },
    shutdown, warnings,
  };
}

test('missing optional config and unselected accessory preserve original setter exactly', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_hook_'));
  try {
    const original = async () => 'original';
    assert.equal(coordinatedSetter(control(folder, RIGHT), original), original);
    writeFileSync(path.join(folder, 'smart-lighting-power.json'), 'invalid config');
    assert.equal(coordinatedSetter(control(folder, 'unselected-third'), original), original);
    const selected = control(folder, RIGHT);
    assert.equal(coordinatedSetter(selected, original), original);
    assert.equal(selected.warnings.length, 1);
  } finally { rmSync(folder, { recursive: true }); }
});

test('existing setter receives original values with power pause before Off over real HTTP', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_hook_'));
  const messages = [];
  let nativeOn = false;
  const server = http.createServer((request, response) => {
    assert.equal(request.url, '/power-owner');
    assert.equal(request.headers['x-power-owner-token'], 'b'.repeat(48));
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      messages.push(JSON.parse(body));
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end(JSON.stringify({ accepted: true, observed: {
        right: { on: nativeOn, sample: performance.now() },
      } }));
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let device;
  try {
    writeFileSync(path.join(folder, 'smart-lighting-power.json'), JSON.stringify({
      version: 1, endpoint: `http://127.0.0.1:${server.address().port}/power-owner`,
      token: 'b'.repeat(48),
    }));
    device = control(folder, RIGHT, false);
    const values = [];
    const setter = coordinatedSetter(device, async value => {
      values.push(value);
      nativeOn = value;
      if (!value) assert.equal(messages.at(-1).states.right, false);
      return 'result';
    });
    assert.equal(await setter(true), 'result');
    assert.equal(messages.at(-1).states.right, true);
    assert.equal(device.cacheState, 'on');
    assert.equal(await setter(false), 'result');
    assert.deepEqual(values, [true, false]);
    assert.equal(device.cacheState, 'off');
    assert.equal(device.shutdown.length, 1);
  } finally {
    if (device) await Promise.all(device.shutdown.map(callback => callback()));
    await new Promise(resolve => server.close(resolve));
    rmSync(folder, { recursive: true });
  }
});

test('native adaptive/color writers cannot bypass a saved Off intent', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_hook_'));
  let device;
  try {
    writeFileSync(path.join(folder, 'smart-lighting-power.json'), JSON.stringify({
      version: 1, endpoint: 'http://127.0.0.1:9/power-owner', token: 'b'.repeat(48),
    }));
    device = control(folder, RIGHT, false);
    const calls = [];
    for (const name of ['internalBrightnessUpdate', 'internalColourUpdate', 'internalCTUpdate']) {
      device[name] = async value => { calls.push([name, value]); };
    }
    coordinatedSetter(device, async () => {});
    await device.internalBrightnessUpdate(50);
    await device.internalColourUpdate(120);
    await device.internalCTUpdate(200);
    assert.deepEqual(calls, []);
  } finally {
    if (device) await Promise.all(device.shutdown.map(callback => callback()));
    rmSync(folder, { recursive: true });
  }
});

test('Off invalidates a debounced brightness intent instead of letting it wake the lamp later', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_hook_'));
  let device;
  let settled;
  let release;
  const held = new Promise(resolve => { release = resolve; });
  try {
    writeFileSync(path.join(folder, 'smart-lighting-power.json'), JSON.stringify({
      version: 1, endpoint: 'http://127.0.0.1:9/power-owner', token: 'b'.repeat(48),
    }));
    device = control(folder, RIGHT, true);
    let writes = 0;
    let began = false;
    device.internalBrightnessUpdate = async function () {
      began = true;
      const key = {};
      this.updateKeyBright = key;
      await held;
      if (this.updateKeyBright === key) writes++;
    };
    const client = { desired: new Map([['right', true]]), confirmed: () => true,
      power: async (id, value, operation) => operation() };
    const setter = coordinatedNativeWrites(device, RIGHT, client, async () => {});
    const pending = device.internalBrightnessUpdate(51);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(began, true);
    const off = setter(false);
    settled = off.catch(() => {});
    release();
    await pending;
    assert.equal(writes, 0);
  } finally {
    release();
    if (device) await Promise.all(device.shutdown.map(callback => callback()));
    if (settled) await settled;
    rmSync(folder, { recursive: true });
  }
});

function nativeFixture(on = true, configure = () => {}) {
  const device = control('/unused', RIGHT, on);
  const native = { on, brightness: 51 };
  const events = [];
  const values = new Map();
  device.cacheState = on ? 'on' : 'off';
  device.cacheBright = 51;
  device.hapChar.Brightness = 'brightness';
  device.service.updateCharacteristic = (name, value) => { values.set(name, value); };
  device.internalBrightnessUpdate = async value => {
    native.brightness = value;
    events.push('brightness:' + value);
  };
  device.internalColourUpdate = async value => { native.hue = value; events.push('color'); };
  device.internalCTUpdate = async value => { native.kelvin = value; events.push('ct'); };
  device.internalSceneUpdate = async () => { native.scene = true; events.push('scene'); };
  let queue = Promise.resolve();
  const client = { desired: new Map([['right', on]]), confirmed: () => true,
    observe: (_id, listener) => { client.observer = listener; },
    power: (id, value, operation) => {
      assert.equal(id, RIGHT);
      client.desired.set('right', value);
      queue = queue.catch(() => {}).then(operation);
      return queue;
    } };
  configure(device, native, events);
  const setter = coordinatedNativeWrites(device, RIGHT, client, async value => {
    await device.beforePower?.(value);
    native.on = value;
    device.cacheState = value ? 'on' : 'off';
    events.push('power:' + value);
  });
  return { device, native, events, values, setter, client };
}

test('paired Home scene color before On waits for the power operation', async () => {
  const { device, native, events, setter } = nativeFixture(false);
  const brightness = device.internalBrightnessUpdate(80);
  const on = setter(true);
  await Promise.all([brightness, on]);
  assert.deepEqual(events, ['power:true', 'brightness:80']);
  assert.equal(native.on, true);
  assert.equal(native.brightness, 80);
});

test('Off drains an already started native writer before sending physical power', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const { device, native, events, setter } = nativeFixture(true, control => {
    control.internalCTUpdate = async () => {
      events.push('ct:start');
      entered();
      await held;
      native.on = true;
      events.push('ct:done');
    };
  });
  const color = device.internalCTUpdate(200);
  await started;
  const off = setter(false);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ['ct:start']);
  release();
  await Promise.all([color, off]);
  assert.deepEqual(events, ['ct:start', 'ct:done', 'power:false']);
  assert.equal(native.on, false);
});

test('old deferred color cannot cross an Off then On boundary', async () => {
  const { device, events, setter } = nativeFixture();
  const color = device.internalColourUpdate(120);
  const off = setter(false);
  const on = setter(true);
  await Promise.all([color, off, on]);
  assert.deepEqual(events, ['power:false', 'power:true']);
});

test('explicit effect On preserves existing scene activation while main Off wins a paired batch', async () => {
  const first = nativeFixture(false);
  await first.device.internalSceneUpdate('scene', 'code', 'ble', true);
  assert.deepEqual(first.events, ['power:true', 'scene']);
  const second = nativeFixture(true);
  const scene = second.device.internalSceneUpdate('scene', 'code', 'ble', true);
  const off = second.setter(false);
  await Promise.all([scene, off]);
  assert.deepEqual(second.events, ['power:false']);
});

test('brightness zero is an Off intent and retains the prior nonzero Home brightness', async () => {
  const { device, native, events, values } = nativeFixture();
  await device.internalBrightnessUpdate(0);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(native.on, false);
  assert.equal(native.brightness, 51);
  assert.deepEqual(events, ['power:false']);
  assert.equal(values.get('brightness'), 51);
});

test('an implicit effect On cannot carry its old scene through a later Off and On', async () => {
  let entered;
  let release;
  let first = true;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const { device, events, setter } = nativeFixture(false, control => {
    control.beforePower = async value => {
      if (value && first) { first = false; entered(); await held; }
    };
  });
  const scene = device.internalSceneUpdate('scene', 'code', 'ble', true);
  await started;
  const off = setter(false);
  const on = setter(true);
  release();
  await Promise.all([scene, off, on]);
  assert.deepEqual(events, ['power:true', 'power:false', 'power:true']);
});

test('failed implicit scene On permits a fresh explicit retry', async () => {
  let attempts = 0;
  const f = nativeFixture(false, device => {
    device.beforePower = async () => { if (++attempts === 1) throw Error('temporarily unavailable'); };
  });
  await assert.rejects(f.device.internalSceneUpdate('scene', 'code', 'ble', true), /unavailable/);
  await f.device.internalSceneUpdate('scene', 'code', 'ble', true);
  assert.equal(attempts, 2);
  assert.deepEqual(f.events, ['power:true', 'scene']);
});

test('old native callbacks cannot overwrite On or other HAP fields across a power epoch', async () => {
  let callback;
  const f = nativeFixture(true, device => {
    device.internalCTUpdate = async () => {
      callback = new Promise(resolve => setTimeout(() => {
        device.service.updateCharacteristic(device.hapChar.On, true);
        device.service.updateCharacteristic('colourMode', true);
        resolve();
      }, 30));
    };
  });
  await f.device.internalCTUpdate(200);
  await f.setter(false);
  await callback;
  assert.equal(f.values.get('on'), false);
  assert.equal(f.values.has('colourMode'), false);
});

test('stale SDK timers cannot clear a newer secondary scene switch', async () => {
  let oldTimer;
  const sceneValues = new Map();
  const secondary = { updateCharacteristic(name, value) { sceneValues.set(name, value); } };
  const f = nativeFixture(true, device => {
    device.usedCodes = ['Scene'];
    device.accessory.getService = () => secondary;
    device.internalCTUpdate = async () => {
      oldTimer = new Promise(resolve => setTimeout(() => {
        secondary.updateCharacteristic('on', false);
        resolve();
      }, 30));
    };
  });
  await f.device.internalCTUpdate(200);
  await f.setter(false);
  await f.setter(true);
  secondary.updateCharacteristic('on', true);
  await oldTimer;
  assert.equal(sceneValues.get('on'), true);
});

test('native status repair never changes the SDK cache during an active power write', async () => {
  let release;
  let entered;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const f = nativeFixture(true, device => {
    device.beforePower = async () => { entered(); await held; assert.equal(device.cacheState, 'on'); };
  });
  const off = f.setter(false);
  await started;
  f.client.observer(false);
  assert.equal(f.device.cacheState, 'on');
  release();
  await off;
  f.client.observer(false);
  assert.equal(f.device.cacheState, 'off');
  assert.equal(f.values.get('on'), false);
});
