// Plugin contracts against the HAP-NodeJS release used by Homebridge 2.5.0.
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import * as hap from '../homebridge/node_modules/@homebridge/hap-nodejs/dist/index.js';
import { IdentifierCache } from '../homebridge/node_modules/@homebridge/hap-nodejs/dist/lib/model/IdentifierCache.js';
import {
  CORE_API, CoreClient, readCredentials, sign, signResponse,
} from '../homebridge/lib/client.js';
import {
  LightsCanCommunicateV2Platform, PLATFORM_NAME, PLUGIN_NAME, UNAVAILABLE_MS,
} from '../homebridge/lib/platform.js';

const LEFT = 'AA:AA:AA:AA:AA:AA:AA:AA';
const RIGHT = 'BB:BB:BB:BB:BB:BB:BB:BB';
const TABLE = 'CC:CC:CC:CC:CC:CC:CC:CC';
const LAMPS = [
  { role: 'left', deviceId: LEFT, name: 'Mykola office L' },
  { role: 'right', deviceId: RIGHT, name: 'Mykola office R' },
];

/** The relevant surface of Homebridge's PlatformAccessory. */
class PlatformAccessory {
  constructor(displayName, UUID) {
    this._associatedHAPAccessory = new hap.Accessory(displayName, UUID);
    this.displayName = displayName;
    this.UUID = UUID;
    this.context = {};
  }

  getService(type) { return this._associatedHAPAccessory.getService(type); }
  addService(...args) { return this._associatedHAPAccessory.addService(...args); }
  configureController(controller) { this._associatedHAPAccessory.configureController(controller); }
}

class FakeClient {
  constructor() {
    this.sent = [];
    this.fail = false;
    this.roles = { left: { on: true, brightness: 40 }, right: { on: false } };
  }

  async set(intent) {
    this.sent.push(intent);
    if (this.fail) throw new Error('Core did not confirm the HomeKit intent (503)');
    return this.roles;
  }

  async state() {
    if (this.fail) throw new Error('Core state unavailable (0)');
    return this.roles;
  }
}

function harness({ cached = [], client = new FakeClient(), lamps = LAMPS, clock } = {}) {
  const events = {};
  const registered = [];
  const unregistered = [];
  const logs = [];
  const api = {
    hap,
    platformAccessory: PlatformAccessory,
    user: { storagePath: () => tmpdir() },
    on: (name, handler) => { events[name] = handler; },
    registerPlatformAccessories: (plugin, platform, list) => registered.push(
      [plugin, platform, list]),
    unregisterPlatformAccessories: (plugin, platform, list) => unregistered.push(
      [plugin, platform, list]),
  };
  const log = { warn: m => logs.push(m), error: m => logs.push(m), info: () => {} };
  const time = { now: clock ?? 0 };
  const platform = new LightsCanCommunicateV2Platform(log, { lamps }, api, {
    client, setInterval: () => 0, now: () => time.now,
  });
  for (const accessory of cached) platform.configureAccessory(accessory);
  return { platform, events, registered, unregistered, logs, client, time };
}

/** An accessory shaped like the cached Govee H6076 (inventory 2026-11-17). */
function goveeAccessory(name, deviceId) {
  const accessory = new hap.Accessory(name, hap.uuid.generate(deviceId));
  const service = accessory.addService(hap.Service.Lightbulb);
  for (const type of [hap.Characteristic.Brightness, hap.Characteristic.Hue,
    hap.Characteristic.Saturation, hap.Characteristic.ColorTemperature]) {
    service.getCharacteristic(type);
  }
  accessory.configureController(new hap.AdaptiveLightingController(service));
  return accessory;
}

function identifiers(accessory) {
  const result = {};
  for (const service of accessory.services) {
    for (const characteristic of service.characteristics) {
      result[`${service.UUID}|${service.subtype ?? ''}|${characteristic.UUID}`] = characteristic.iid;
    }
  }
  return result;
}

test('the plugin reuses the AIDs and IIDs that the Govee accessories had on the bridge', async () => {
  const before = new hap.Bridge('Govee', hap.uuid.generate('govee-child-bridge'));
  const goveeLeft = goveeAccessory('Mykola office L', LEFT);
  const goveeRight = goveeAccessory('Mykola office R', RIGHT);
  before.addBridgedAccessories([goveeAccessory('Table Lamp', TABLE), goveeLeft, goveeRight]);
  const cache = new IdentifierCache('0E:00:00:00:00:01');
  before._assignIDs(cache);
  const saved = JSON.parse(JSON.stringify(cache._cache));

  const { platform } = harness();
  await platform.start();
  const after = new hap.Bridge('Govee', hap.uuid.generate('govee-child-bridge'));
  const leftAccessory = platform.lamps.get('left').accessory._associatedHAPAccessory;
  const rightAccessory = platform.lamps.get('right').accessory._associatedHAPAccessory;
  after.addBridgedAccessories([leftAccessory, rightAccessory]);
  const reloaded = new IdentifierCache('0E:00:00:00:00:01');
  reloaded._cache = JSON.parse(JSON.stringify(saved));
  after._assignIDs(reloaded);

  assert.equal(leftAccessory.UUID, goveeLeft.UUID);
  assert.equal(leftAccessory.aid, goveeLeft.aid);
  assert.equal(rightAccessory.aid, goveeRight.aid);
  for (const [goveeAcc, ours] of [[goveeLeft, leftAccessory], [goveeRight, rightAccessory]]) {
    const expected = identifiers(goveeAcc);
    const actual = identifiers(ours);
    for (const [key, iid] of Object.entries(expected)) {
      assert.equal(actual[key], iid, `IID changed for ${key}`);
    }
    // No identifier was created for these lamps beyond the cached ones.
    const added = Object.keys(reloaded._cache).filter(key => key.startsWith(ours.UUID)
      && !(key in saved));
    assert.deepEqual(added, []);
  }
});

test('new lamps register with the Govee-derived UUID; stale cache entries are removed', async () => {
  const stale = new PlatformAccessory('Old', hap.uuid.generate('stale'));
  const cachedLeft = new PlatformAccessory('Mykola office L', hap.uuid.generate(LEFT));
  const { platform, registered, unregistered } = harness({ cached: [stale, cachedLeft] });
  await platform.start();
  assert.equal(registered.length, 1);
  assert.deepEqual(registered[0].slice(0, 2), [PLUGIN_NAME, PLATFORM_NAME]);
  assert.deepEqual(registered[0][2].map(a => a.UUID), [hap.uuid.generate(RIGHT)]);
  assert.deepEqual(unregistered[0][2], [stale]);
  assert.equal(platform.lamps.get('left').accessory, cachedLeft);
  assert.equal(cachedLeft.context.deviceId, LEFT);
});

test('invalid lamp configuration does not start or register anything', async () => {
  for (const lamps of [[], [{ ...LAMPS[0], role: 'strip' }], [LAMPS[0], { ...LAMPS[1], role: 'left' }],
    [{ ...LAMPS[0], deviceId: 'aa:aa' }]]) {
    const { platform, registered, logs } = harness({ lamps });
    await platform.start();
    assert.equal(registered.length, 0);
    assert.match(logs[0], /Not started/);
  }
});

test('HomeKit writes become core intents; hue and saturation are sent as one color', async () => {
  const { platform, client } = harness();
  await platform.start();
  const service = platform.lamps.get('left').service;
  await service.getCharacteristic(hap.Characteristic.On).handleSetRequest(false);
  await service.getCharacteristic(hap.Characteristic.Brightness).handleSetRequest(55);
  await Promise.all([
    service.getCharacteristic(hap.Characteristic.Hue).handleSetRequest(120),
    service.getCharacteristic(hap.Characteristic.Saturation).handleSetRequest(80),
  ]);
  await service.getCharacteristic(hap.Characteristic.ColorTemperature).handleSetRequest(300);
  assert.deepEqual(client.sent, [
    { role: 'left', on: false },
    { role: 'left', brightness: 55 },
    { role: 'left', hue: 120, saturation: 80 },
    { role: 'left', color_temperature: 300 },
  ]);
});

test('an unconfirmed intent is a HomeKit communication failure', async () => {
  const { platform, client } = harness();
  await platform.start();
  client.fail = true;
  const on = platform.lamps.get('right').service.getCharacteristic(hap.Characteristic.On);
  // HAP converts the thrown HapStatusError into the status sent to the controller.
  await assert.rejects(on.handleSetRequest(true),
    error => error === hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
});

test('polling mirrors the core state and marks the lamps unavailable only after a timeout', async () => {
  const { platform, client, time } = harness();
  await platform.start();
  const service = platform.lamps.get('left').service;
  assert.equal(service.getCharacteristic(hap.Characteristic.On).value, true);
  assert.equal(service.getCharacteristic(hap.Characteristic.Brightness).value, 40);
  client.roles.left = { on: false, brightness: null, hue: 30, saturation: 100 };
  await platform.poll();
  assert.equal(service.getCharacteristic(hap.Characteristic.On).value, false);
  assert.equal(service.getCharacteristic(hap.Characteristic.Brightness).value, 40);
  assert.equal(service.getCharacteristic(hap.Characteristic.Hue).value, 30);
  client.fail = true;
  time.now += UNAVAILABLE_MS - 1;
  await platform.poll();
  assert.equal(service.getCharacteristic(hap.Characteristic.On).statusCode, 0);
  time.now += 1;
  await platform.poll();
  assert.equal(service.getCharacteristic(hap.Characteristic.On).statusCode,
    hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
});

test('requests are signed and never carry the credential itself', async () => {
  assert.equal(sign('b'.repeat(64), 'POST', '/homekit', '1000.000', 'c'.repeat(32),
    JSON.stringify({ role: 'left', on: true })),
  '7f700addbd1e66dda66bc369cb670882c6e92ce71c92c587658fbf9e2b21fa64');
  const token = 'b'.repeat(64);
  const seen = [];
  const fetch = async (url, init) => {
    seen.push({ url, init });
    const body = JSON.stringify({ accepted: true, version: 1, roles: {} });
    return new Response(body, { headers: {
      'X-LCC-Response': signResponse(token, init.headers['X-LCC-Nonce'], 200, body) } });
  };
  const client = new CoreClient({ endpoint: 'http://192.168.99.10:19880', token },
    { fetch, now: () => 1000000 });
  await client.set({ role: 'right', on: true });
  await client.set({ role: 'right', on: true });
  const [first, second] = seen;
  assert.equal(first.url, 'http://192.168.99.10:19880/homekit');
  assert.ok(!JSON.stringify(first.init).includes(token));
  const headers = first.init.headers;
  assert.equal(headers['X-LCC-Timestamp'], '1000.000');
  assert.equal(headers['X-LCC-Signature'], sign(token, 'POST', '/homekit', '1000.000',
    headers['X-LCC-Nonce'], first.init.body));
  assert.notEqual(headers['X-LCC-Nonce'], second.init.headers['X-LCC-Nonce']);
});

test('unsigned, altered or replayed core responses are not trusted', async () => {
  const token = 'b'.repeat(64);
  const forged = JSON.stringify({ accepted: true, version: 1, roles: { right: { on: true } } });
  let answer;
  const fetch = async (url, init) => answer(init.headers['X-LCC-Nonce']);
  const client = new CoreClient({ endpoint: 'http://192.168.99.10:19880', token }, { fetch });
  answer = () => new Response(forged);
  await assert.rejects(client.set({ role: 'right', on: true }), /not authentic/);
  answer = nonce => new Response(forged, { headers: {
    'X-LCC-Response': signResponse(token, nonce, 200, '{"accepted":true}') } });
  await assert.rejects(client.set({ role: 'right', on: true }), /not authentic/);
  answer = () => new Response(forged, { headers: {
    'X-LCC-Response': signResponse(token, 'd'.repeat(32), 200, forged) } });
  await assert.rejects(client.state(), /not authentic/);
});

test('a core with another HomeKit API version is refused before any write', async () => {
  const token = 'b'.repeat(64);
  let version = CORE_API + 1;
  const fetch = async (url, init) => {
    const body = JSON.stringify({ version, roles: { right: { on: true } } });
    return new Response(body, { headers: {
      'X-LCC-Response': signResponse(token, init.headers['X-LCC-Nonce'], 200, body) } });
  };
  const client = new CoreClient({ endpoint: 'http://192.168.99.10:19880', token }, { fetch });
  await assert.rejects(client.state(), /HomeKit API 2 is not supported \(plugin expects 1\)/);
  version = CORE_API;
  assert.deepEqual(await client.state(), { right: { on: true } });
});

test('the core credential must be a private, well-formed file', () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'tmp_plugin_credentials_'));
  try {
    const file = path.join(dir, 'lights-can-communicate-v2.json');
    const write = (value, mode = 0o600) => {
      writeFileSync(file, JSON.stringify(value));
      chmodSync(file, mode);
    };
    const token = 'b'.repeat(64);
    write({ version: 1, endpoint: 'http://192.168.99.10:19880', token });
    assert.deepEqual(readCredentials(file), { endpoint: 'http://192.168.99.10:19880', token });
    for (const [value, mode] of [
      [{ version: 1, endpoint: 'http://192.168.99.10:19880', token }, 0o644],
      [{ version: 1, endpoint: 'http://192.168.99.10:19880/x', token }, 0o600],
      [{ version: 1, endpoint: 'http://user:PRIVATE@host:1', token }, 0o600],
      [{ version: 1, endpoint: 'http://192.168.99.10:19880', token: 'PRIVATE' }, 0o600],
      [{ version: 1, endpoint: 'http://8.8.8.8:19880', token }, 0o600],
      [{ version: 1, endpoint: 'http://core.example:19880', token }, 0o600],
    ]) {
      write(value, mode);
      assert.throws(() => readCredentials(file), error => !error.message.includes('PRIVATE'));
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('the rehearsal reports exact reuse and flags missing or new identifiers', async () => {
  const { rehearse } = await import('../homebridge/tools/rehearse-identity.mjs');
  const bridge = new hap.Bridge('Govee', hap.uuid.generate('govee-child-bridge'));
  bridge.addBridgedAccessories([goveeAccessory('Mykola office L', LEFT),
    goveeAccessory('Mykola office R', RIGHT), goveeAccessory('Table Lamp', TABLE)]);
  const cache = new IdentifierCache('0E:00:00:00:00:02');
  bridge._assignIDs(cache);
  const report = rehearse(cache._cache, LAMPS);
  assert.deepEqual(report.map(item => [item.known, item.added, item.unused]),
    [[true, [], []], [true, [], []]]);
  // A cached characteristic the plugin does not publish would lose its IID.
  const extra = `${hap.uuid.generate(LEFT)}|${hap.Service.Lightbulb.UUID}|0000FFFF-0000-1000-8000-0026BB765291`;
  const changed = rehearse({ ...cache._cache, [extra]: 99 }, LAMPS);
  assert.deepEqual(changed[0].unused, [extra]);
  const unknown = rehearse({}, LAMPS);
  assert.equal(unknown[0].known, false);
});
