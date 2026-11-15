import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { PowerOwnerClient } from '../experiments/homebridge-power/owner-client.mjs';

const RIGHT = 'D4:A5:D2:21:C0:46:4F:55';
const THIRD = '00:00:00:00:00:00:00:03';

function fixture(options = {}) {
  let now = 100;
  const sent = [];
  const sleeps = [];
  const client = new PowerOwnerClient({
    endpoint: 'http://127.0.0.1:19880/power-owner', token: 'a'.repeat(48),
    ...options,
  }, {
    clock: () => now,
    wallClock: () => 1000000 + now,
    sleep: async ms => { sleeps.push(ms); now += ms; },
    request: async message => { sent.push(structuredClone(message)); return { accepted: true }; },
    ...options.dependencies,
  });
  return { client, sent, sleeps };
}

test('owner Off is acknowledged before original handler and heartbeats keep it paused', async () => {
  const { client, sent } = fixture();
  client.register(RIGHT, true);
  await client.heartbeat();
  await client.power(RIGHT, false, async () => {
    assert.equal(sent.at(-1).states.right, false);
    await client.heartbeat();
    assert.equal(sent.at(-1).states.right, false);
  });
  assert.equal(sent[0].states.right, true);
  assert.equal(sent.at(-1).states.right, false);
  await client.close();
});

test('On grants follow original success and failure keeps Off intent', async () => {
  const { client, sent } = fixture();
  client.register(RIGHT, false);
  await client.power(RIGHT, true, async () => {
    await client.heartbeat();
    assert.equal(sent.at(-1).states.right, false);
  });
  assert.equal(sent.at(-1).states.right, true);
  await client.power(RIGHT, false, async () => {});
  await assert.rejects(client.power(RIGHT, true, async () => { throw Error('device unavailable'); }));
  await client.heartbeat();
  assert.equal(sent.at(-1).states.right, false);
  await client.close();
});

test('unreachable coordinator expires old grants before still performing Home Off', async () => {
  let offline = false;
  const { client, sleeps } = fixture({ dependencies: {
    request: async () => { if (offline) throw Error('unavailable'); return { accepted: true }; },
  } });
  client.register(RIGHT, true);
  await client.heartbeat();
  offline = true;
  let off = false;
  await client.power(RIGHT, false, async () => {
    assert.ok(sleeps.reduce((a, b) => a + b, 0) >= 2900);
    off = true;
  });
  assert.equal(off, true);
  await client.close();
});

test('concurrent Off then On keep physical operation order', async () => {
  const { client, sent } = fixture();
  client.register(RIGHT, true);
  let finishOff;
  const held = new Promise(resolve => { finishOff = resolve; });
  const events = [];
  const off = client.power(RIGHT, false, async () => { events.push('off'); await held; });
  const on = client.power(RIGHT, true, async () => { events.push('on'); });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(events, ['off']);
  assert.equal(sent.at(-1).states.right, false);
  finishOff();
  await Promise.all([off, on]);
  assert.deepEqual(events, ['off', 'on']);
  assert.equal(sent.at(-1).states.right, true);
  await client.close();
});

test('unselected third accessory is passed through without coordination', async () => {
  const { client, sent } = fixture();
  assert.equal(client.register(THIRD, true), false);
  assert.equal(await client.power(THIRD, false, async () => 'original'), 'original');
  assert.deepEqual(sent, []);
  await client.close();
});

test('accepted Off intent survives client restart despite stale Home cache On', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_owner_'));
  try {
    const statePath = path.join(folder, 'power-state.json');
    const first = fixture({ statePath });
    first.client.register(RIGHT, true);
    await first.client.power(RIGHT, false, async () => {});
    await first.client.close();
    assert.deepEqual(JSON.parse(readFileSync(statePath, 'utf8')), { right: false });
    const second = fixture({ statePath });
    second.client.register(RIGHT, true);
    await second.client.heartbeat();
    assert.equal(second.sent.at(-1).states.right, false);
    await second.client.close();
  } finally { rmSync(folder, { recursive: true }); }
});

test('new owner with unavailable coordinator waits out grants from previous process', async () => {
  const { client, sleeps } = fixture({ dependencies: {
    request: async () => { throw Error('unavailable'); },
  } });
  client.register(RIGHT, true);
  await client.power(RIGHT, false, async () => {
    assert.ok(sleeps.reduce((a, b) => a + b, 0) >= 2900);
  });
  await client.close();
});

test('a live conflicting owner cannot be bypassed by an Off fallback', async () => {
  const { client } = fixture({ dependencies: { request: async () => ({ accepted: false }) } });
  client.register(RIGHT, true);
  let off = false;
  await assert.rejects(client.power(RIGHT, false, async () => { off = true; }));
  assert.equal(off, false);
  await client.close();
});

test('shutdown cancels a queued power write instead of treating a closed client as Off ack', async () => {
  const { client, sent } = fixture();
  client.register(RIGHT, true);
  let release;
  let entered;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const on = client.power(RIGHT, true, async () => { entered(); await held; });
  let offCalled = false;
  const off = client.power(RIGHT, false, async () => { offCalled = true; });
  const rejected = assert.rejects(off, /closed/);
  await started;
  await client.close();
  release();
  await on;
  await rejected;
  assert.equal(offCalled, false);
  assert.equal(sent.at(-1).states.right, false);
});

test('failed atomic state replacement leaves no temporary files or physical command', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_owner_'));
  try {
    const statePath = path.join(folder, 'power-state');
    mkdirSync(statePath);
    const { client } = fixture({ statePath });
    client.register(RIGHT, true);
    let offCalled = false;
    await assert.rejects(client.power(RIGHT, false, async () => { offCalled = true; }));
    assert.equal(offCalled, false);
    assert.deepEqual(readdirSync(folder), ['power-state']);
    await client.close();
  } finally { rmSync(folder, { recursive: true }); }
});

test('an explicit rejection is not forgotten when a retry loses connectivity', async () => {
  let requests = 0;
  const { client } = fixture({ dependencies: { request: async () => {
    if (++requests === 1) return { accepted: false };
    throw Error('network unavailable after rejection');
  } } });
  client.register(RIGHT, true);
  let off = false;
  await assert.rejects(client.power(RIGHT, false, async () => { off = true; }));
  assert.equal(off, false);
  await client.close();
});

test('shutdown while pause acknowledgement is pending never starts the physical setter', async () => {
  let finish;
  let requests = 0;
  const pending = new Promise(resolve => { finish = resolve; });
  const { client } = fixture({ dependencies: { request: async () =>
    ++requests === 1 ? pending : { accepted: true } } });
  client.register(RIGHT, true);
  let off = false;
  const task = client.power(RIGHT, false, async () => { off = true; });
  const rejected = assert.rejects(task, /closed/);
  await new Promise(resolve => setImmediate(resolve));
  await client.close();
  finish({ accepted: true });
  await rejected;
  assert.equal(off, false);
});

test('HTTP rejections and malformed acknowledgements never use offline power fallback', async () => {
  for (const [status, body] of [[400, '{}'], [403, '{}'], [503, '{}'], [200, 'null']]) {
    const server = http.createServer((request, response) => {
      request.resume();
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(body);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    let now = 100;
    const client = new PowerOwnerClient({
      endpoint: `http://127.0.0.1:${server.address().port}/power-owner`, token: 'a'.repeat(48),
    }, { clock: () => now, sleep: async ms => { now += ms; } });
    try {
      client.register(RIGHT, true);
      let off = false;
      await assert.rejects(client.power(RIGHT, false, async () => { off = true; }));
      assert.equal(off, false);
    } finally {
      await client.close();
      await new Promise(resolve => server.close(resolve));
    }
  }
});

test('corrupted existing state cannot grant cached Home On permission', async () => {
  const folder = mkdtempSync(path.join(tmpdir(), 'tmp_power_owner_'));
  try {
    const statePath = path.join(folder, 'power-state.json');
    for (const value of ['5', 'true', 'null', '[]', '{}', '{"right":1}']) {
      writeFileSync(statePath, value);
      const { client, sent } = fixture({ statePath });
      client.register(RIGHT, true);
      await client.heartbeat();
      assert.equal(sent.at(-1).states.right, false, value);
      await client.close();
    }
  } finally { rmSync(folder, { recursive: true }); }
});

test('real HTTP replay guard accepts overlapping timer and verification callers in wire order', async () => {
  let latest = 0;
  let active = 0;
  let peak = 0;
  const server = http.createServer((request, response) => {
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      const message = JSON.parse(body);
      active++;
      peak = Math.max(peak, active);
      setTimeout(() => {
        const accepted = message.sequence > latest;
        latest = Math.max(latest, message.sequence);
        active--;
        response.writeHead(200, { 'Content-Type': 'application/json' });
        response.end(JSON.stringify({ accepted }));
      }, message.sequence === 1 ? 60 : 0);
    });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const client = new PowerOwnerClient({ endpoint: `http://127.0.0.1:${server.address().port}/power-owner`,
    token: 'a'.repeat(48) });
  client.register(RIGHT, true);
  try {
    await Promise.all([client.heartbeat(), client.heartbeat()]);
    assert.equal(peak, 1);
    assert.equal(latest, 2);
  } finally {
    await client.close();
    await new Promise(resolve => server.close(resolve));
  }
});
