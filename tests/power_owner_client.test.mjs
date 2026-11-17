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

const REQUEST_ID = '1'.repeat(32);
const turn = () => new Promise(resolve => setImmediate(resolve));

function remoteFixture(options = {}) {
  let now = 1000000;
  let sample = 0;
  let nativeOn = false;
  let commands = [];
  let acks = [];
  const sent = [];
  const writes = [];
  const client = new PowerOwnerClient({
    endpoint: 'http://127.0.0.1:19880/power-owner', token: 'a'.repeat(48), confirmReadback: true,
    ...options.config,
  }, {
    clock: () => now, wallClock: () => now,
    sleep: async ms => { now += ms; await options.onSleep?.(); },
    request: async message => {
      sent.push(structuredClone(message));
      const response = { accepted: true, power_request_version: 1, observed: { right: {
        on: options.unknown ? null : nativeOn, sample: options.unknown ? null : ++sample,
      } }, power_requests: commands, power_request_acks: acks };
      commands = [];
      acks = [];
      return response;
    },
  });
  client.register(RIGHT, false);
  const handler = (value, guard) => client.power(RIGHT, value, async () => {
    guard();
    await options.beforeWrite?.(value);
    guard();
    if (!options.dropWrite) nativeOn = value;
    writes.push(value);
  }, async () => { guard(); nativeOn = value; writes.push(value); }, guard);
  return { client, sent, writes, handler,
    deliver: (patch = {}) => { commands = [{ id: REQUEST_ID, role: 'right', on: true,
      issued_at: now / 1000, expires_at: now / 1000 + 12, revision: 0, ...patch }]; },
    envelope: (value, acknowledgements = []) => { commands = value; acks = acknowledgements; },
    advance: ms => { now += ms; },
    homeOff: () => client.power(RIGHT, false, async () => { nativeOn = false; writes.push(false); }),
  };
}

async function remoteResult(fixture) {
  for (let attempt = 0; attempt < 40; attempt++) {
    await turn();
    await fixture.client.heartbeat();
    const results = fixture.sent.at(-1).power_requests?.results;
    if (results?.length) return results[0];
  }
  assert.fail('remote power result was not advertised');
}

test('remote power uses the registered setter and native confirmation outside the heartbeat queue', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  assert.equal(f.sent[0].power_requests, undefined);
  assert.deepEqual(f.sent[1].power_requests, { version: 1, revisions: { right: 0 }, results: [] });
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: true });
  assert.deepEqual(f.writes, [true]);
  assert.equal(f.sent.at(-1).power_requests.revisions.right, 1);
  await f.client.close();
});

test('duplicate commands execute once and ACK stops result advertising without replaying', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await remoteResult(f);
  f.deliver();
  await f.client.heartbeat();
  f.envelope([], [REQUEST_ID]);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await turn();
  await f.client.heartbeat();
  assert.deepEqual(f.writes, [true]);
  assert.deepEqual(f.sent.at(-1).power_requests.results, []);
  await f.client.close();
});

test('ordinary Home Off synchronously supersedes remote On before dispatch', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  const off = f.homeOff();
  await off;
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: false });
  assert.deepEqual(f.writes, [false]);
  assert.equal(f.sent.at(-1).power_requests.revisions.right, 1);
  await f.client.close();
});

test('ordinary Home Off during remote operation prevents a stale physical write', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const f = remoteFixture({ beforeWrite: async () => { entered(); await held; } });
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await started;
  const off = f.homeOff();
  release();
  await off;
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: false });
  assert.deepEqual(f.writes, [false]);
  await f.client.close();
});

test('expired remote request waiting for an existing role operation never writes', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  let release;
  let entered;
  const held = new Promise(resolve => { release = resolve; });
  const started = new Promise(resolve => { entered = resolve; });
  const existing = f.client.power(RIGHT, false, async () => { entered(); await held; });
  await started;
  f.deliver({ revision: 1 });
  await f.client.heartbeat();
  f.advance(12001);
  release();
  await existing;
  await turn();
  assert.deepEqual(f.writes, []);
  await f.client.close();
});

test('unavailable native telemetry never allows remote power success or physical dispatch', async () => {
  const f = remoteFixture({ unknown: true });
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: false });
  assert.deepEqual(f.writes, []);
  await f.client.close();
});

test('malformed remote envelopes and unknown roles fail before any physical writes', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  for (const patch of [{ role: 'third' }, { extra: true }, { revision: -1 },
    { expires_at: 1013 }, { on: 1 }, { id: 'A'.repeat(32) }]) {
    await f.client.heartbeat();
    f.deliver(patch);
    await assert.rejects(f.client.heartbeat());
  }
  await f.client.heartbeat();
  f.envelope([], ['bad']);
  await assert.rejects(f.client.heartbeat());
  await turn();
  assert.deepEqual(f.writes, []);
  await f.client.close();
});

test('legacy clients advertise no capability and never dispatch unsolicited remote work', async () => {
  const f = remoteFixture();
  f.deliver();
  await f.client.heartbeat();
  await turn();
  assert.equal(f.sent[0].power_requests, undefined);
  assert.deepEqual(f.writes, []);
  assert.equal(f.client.registerPowerHandler(THIRD, f.handler), false);
  await f.client.close();
});

test('shutdown cancels remote work before dispatch', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await f.client.close();
  await turn();
  assert.deepEqual(f.writes, []);
});

test('busy remote role fails another command promptly while the first native write is pending', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const f = remoteFixture({ beforeWrite: async () => { entered(); await held; } });
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await started;
  const secondId = '3'.repeat(32);
  f.deliver({ id: secondId, revision: 1 });
  await f.client.heartbeat();
  for (let i = 0; i < 3; i++) await turn();
  await f.client.heartbeat();
  const results = f.sent.at(-1).power_requests.results;
  release();
  assert.deepEqual(results, [{ id: secondId, success: false }]);
  await remoteResult(f);
  await f.client.close();
});

test('64 retained replay fences reject overflow after ACK and recover capacity only after expiry', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  const ids = [];
  for (let i = 0; i < 64; i++) {
    const id = i.toString(16).padStart(32, '0');
    ids.push(id);
    f.deliver({ id, revision: 99 });
    await f.client.heartbeat();
    await turn();
  }
  await f.client.heartbeat();
  assert.equal(f.sent.at(-1).power_requests.results.length, 64);
  f.envelope([], ids);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  await turn();
  assert.deepEqual(f.writes, []);
  f.advance(12001);
  f.deliver();
  await f.client.heartbeat();
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: true });
  assert.deepEqual(f.writes, [true]);
  await f.client.close();
});

test('future and nonconfirming requests fail without physical power', async () => {
  for (const options of [{}, { config: { confirmReadback: false } }]) {
    const f = remoteFixture(options);
    f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
    f.deliver(options.config ? {} : { issued_at: 1000.501, expires_at: 1012 });
    await f.client.heartbeat();
    assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: false });
    assert.deepEqual(f.writes, []);
    await f.client.close();
  }
});


test('a later Home intent stops a stale remote retry while opposite telemetry remains fresh', async () => {
  let off;
  let sleeps = 0;
  const f = remoteFixture({ dropWrite: true, onSleep: async () => {
    if (++sleeps === 3) off = f.homeOff();
  } });
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  f.deliver();
  await f.client.heartbeat();
  assert.deepEqual(await remoteResult(f), { id: REQUEST_ID, success: false });
  await off;
  assert.deepEqual(f.writes, [true, false]);
  await f.client.close();
});

test('present remote envelope fields require arrays and enforce command and ACK limits', async () => {
  const f = remoteFixture();
  f.client.registerPowerHandler(RIGHT, f.handler);
  await f.client.heartbeat();
  for (const [commands, acks] of [[null, []], [{}, []], [[], null], [[], {}],
    [[], Array.from({ length: 65 }, (_, i) => i.toString(16).padStart(32, '0'))],
    [[null], []], [[{}, {}, {}], []], [[], [REQUEST_ID, REQUEST_ID]]]) {
    await f.client.heartbeat();
    f.envelope(commands, acks);
    await assert.rejects(f.client.heartbeat());
  }
  assert.deepEqual(f.writes, []);
  await f.client.close();
});

test('new owner with registered handler keeps legacy envelopes and independent Home power with old core', async () => {
  const sent = [];
  const writes = [];
  let nativeOn = false;
  let sample = 0;
  const { client } = fixture({ confirmReadback: true, dependencies: { request: async message => {
    sent.push(structuredClone(message));
    // The previous core rejects any additional heartbeat fields.
    return { accepted: Object.keys(message).sort().join(',') === 'issued_at,sequence,session,states',
      observed: { right: { on: nativeOn, sample: ++sample } } };
  } } });
  client.register(RIGHT, false);
  client.registerPowerHandler(RIGHT, async () => { assert.fail('old core cannot dispatch remote power'); });
  await client.heartbeat();
  await client.power(RIGHT, true, async () => { nativeOn = true; writes.push(true); });
  await client.power(RIGHT, false, async () => { nativeOn = false; writes.push(false); });
  assert.deepEqual(writes, [true, false]);
  assert.ok(sent.every(message => !Object.hasOwn(message, 'power_requests')));
  await client.close();
});

test('only an exact accepted version one enables remote capability and initial commands are ignored', async () => {
  for (const version of [undefined, null, true, '1', 2, 1]) {
    const sent = [];
    let calls = 0;
    const { client } = fixture({ confirmReadback: true, dependencies: {
      request: async message => {
        sent.push(structuredClone(message));
        return { accepted: true, ...(version === undefined ? {} : { power_request_version: version }),
          power_requests: [{ id: REQUEST_ID, role: 'right', on: true,
            issued_at: 1000.1, expires_at: 1012, revision: 0 }] };
      },
    } });
    client.register(RIGHT, false);
    client.registerPowerHandler(RIGHT, async () => { calls++; });
    await client.heartbeat();
    await turn();
    assert.equal(calls, 0);
    assert.equal(sent[0].power_requests, undefined);
    await client.heartbeat();
    assert.equal(Object.hasOwn(sent[1], 'power_requests'), version === 1);
    await client.close();
  }
});
