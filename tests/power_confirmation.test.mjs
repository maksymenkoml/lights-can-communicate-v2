import assert from 'node:assert/strict';
import test from 'node:test';
import { PowerOwnerClient } from '../experiments/homebridge-power/owner-client.mjs';

const RIGHT = 'D4:A5:D2:21:C0:46:4F:55';

function fixture(options = {}) {
  let now = 0;
  let native = options.native ?? true;
  const messages = [];
  const warnings = [];
  const state = { get now() { return now; }, get native() { return native; },
    set native(value) { native = value; } };
  const client = new PowerOwnerClient({ endpoint: 'http://127.0.0.1:19880/power-owner',
    token: 'a'.repeat(48), confirmReadback: true }, {
    clock: () => now, wallClock: () => 1000000 + now,
    sleep: async ms => { now += ms; await options.sleep?.(state, ms); },
    warn: message => warnings.push(message),
    request: async message => {
      messages.push(structuredClone(message));
      if (options.request) return options.request(state, message);
      return { accepted: true, observed: {
        right: { on: native, sample: Math.floor(now / 150) / 10 },
      } };
    },
  });
  client.register(RIGHT, options.native ?? true);
  return { client, messages, state, warnings };
}

test('dropped Off is retried through the original power sender while colors stay blocked', async () => {
  const { client, messages, state } = fixture();
  let retries = 0;
  try {
    const result = await client.power(RIGHT, false, async () => 'original', async () => {
      retries++;
      assert.equal(messages.at(-1).states.right, false);
      state.native = false;
    });
    assert.equal(result, 'original');
    assert.equal(retries, 1);
    assert.equal(state.native, false);
    assert.ok(state.now >= 1500);
    assert.equal(client.confirmed(RIGHT), true);
    assert.ok(messages.every(message => message.states.right === false));
  } finally { await client.close(); }
});

test('late implicit On restarts Off settling and requires another physical Off', async () => {
  let reactivated = false;
  const { client, state } = fixture({ sleep: async device => {
    if (!reactivated && device.now >= 800) { device.native = true; reactivated = true; }
  } });
  let retries = 0;
  try {
    await client.power(RIGHT, false, async () => { state.native = false; }, async () => {
      retries++;
      state.native = false;
    });
    assert.equal(reactivated, true);
    assert.equal(retries, 1);
    assert.ok(state.now >= 2000);
    assert.equal(state.native, false);
  } finally { await client.close(); }
});

test('normal delayed Off telemetry is allowed to converge without restarting its transition', async () => {
  const { client, state } = fixture({ sleep: async device => {
    if (device.now >= 4800) device.native = false;
  } });
  let writes = 0;
  try {
    await client.power(RIGHT, false, async () => { writes++; }, async () => { writes++; });
    assert.equal(writes, 1);
    assert.equal(client.confirmed(RIGHT), true);
    assert.ok(state.now >= 6000 && state.now <= 8500);
  } finally { await client.close(); }
});

test('On grants follow distinct fresh readbacks and a dropped On is retried', async () => {
  const { client, state, messages } = fixture({ native: false });
  let retries = 0;
  try {
    await client.power(RIGHT, true, async () => {}, async () => {
      assert.ok(messages.every(message => message.states.right === false));
      state.native = true;
      retries++;
    });
    assert.equal(retries, 1);
    assert.equal(messages.at(-1).states.right, true);
    assert.equal(client.confirmed(RIGHT), true);
  } finally { await client.close(); }
});

test('persistently opposite power has bounded retries and never releases colors', async () => {
  const { client, messages, state } = fixture();
  let writes = 0;
  try {
    await assert.rejects(client.power(RIGHT, false, async () => { writes++; },
      async () => { writes++; }), /confirm/i);
    assert.equal(writes, 3);
    assert.ok(state.now < 10000);
    assert.ok(messages.every(message => message.states.right === false));
    assert.equal(client.confirmed(RIGHT), false);
  } finally { await client.close(); }
});

test('unknown or repeated readbacks cannot confirm power or authorize another write', async () => {
  for (const observed of [{ on: null, sample: null }, { on: false, sample: 1 }]) {
    const { client } = fixture({ request: async () => ({ accepted: true,
      observed: { right: observed } }) });
    let writes = 0;
    try {
      await assert.rejects(client.power(RIGHT, false, async () => { writes++; },
        async () => { writes++; }), /confirm/i);
      assert.equal(writes, 1);
      assert.equal(client.confirmed(RIGHT), false);
    } finally { await client.close(); }
  }
});

test('unavailable coordinator retains independent original Home power after lease expiry', async () => {
  const { client, state, warnings } = fixture({ request: async () => { throw Error('offline'); } });
  let writes = 0;
  try {
    const result = await client.power(RIGHT, false, async () => { writes++; return 'original'; },
      async () => { writes++; });
    assert.equal(result, 'original');
    assert.equal(writes, 1);
    assert.ok(state.now >= 2900);
    assert.equal(client.confirmed(RIGHT), false);
    assert.equal(warnings.length, 1);
  } finally { await client.close(); }
});

test('malformed or explicitly rejected readbacks never use offline confirmation fallback', async () => {
  for (const response of [{ accepted: true }, { accepted: true, observed: {
    right: { on: 'off', sample: 1 },
  } }, { accepted: false }]) {
    let count = 0;
    const { client, warnings } = fixture({ request: async () => {
      if (++count === 1) return { accepted: true };
      return response;
    } });
    let retries = 0;
    try {
      await assert.rejects(client.power(RIGHT, false, async () => {}, async () => { retries++; }));
      assert.equal(retries, 0);
      assert.equal(warnings.length, 0);
    } finally { await client.close(); }
  }
});

test('network loss cannot forget a known opposite physical state', async () => {
  let count = 0;
  const { client, warnings } = fixture({ request: async () => {
    if (++count === 1) return { accepted: true, observed: { right: { on: true, sample: 0 } } };
    if (count === 2) return { accepted: true, observed: { right: { on: true, sample: 1 } } };
    throw Error('network unavailable after negative readback');
  } });
  try {
    await assert.rejects(client.power(RIGHT, false, async () => {}, async () => {}), /confirm/i);
    assert.equal(warnings.length, 0);
  } finally { await client.close(); }
});

test('offline On cannot grant color after reconnect until native On is proved', async () => {
  let offline = true;
  const { client, state, messages } = fixture({ native: false, request: async device => {
    if (offline) throw Error('offline');
    return { accepted: true, observed: {
      right: { on: device.native, sample: device.now / 1000 },
    } };
  } });
  try {
    await client.power(RIGHT, true, async () => {}, async () => {});
    offline = false;
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, false);
    state.native = true;
    await client.sleep(150);
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, false);
    await client.sleep(150);
    await client.heartbeat();
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, true);
  } finally { await client.close(); }
});

test('delayed old On replies cannot overrule a newer native Off response', async () => {
  const pending = new Map();
  const { client, messages } = fixture({ request: async (device, message) => {
    if (message.sequence <= 3) return new Promise(resolve => { pending.set(message.sequence, resolve); });
    return { accepted: true, observed: { right: { on: false, sample: device.now / 1000 } } };
  } });
  // Exercise the lower-level response fence even though production heartbeats
  // now serialize their requests before reaching this sender.
  client.heartbeat = (observations = false) =>
    client.send(Object.fromEntries(client.states), false, observations);
  try {
    const first = client.heartbeat();
    await client.sleep(150);
    const second = client.heartbeat();
    await client.sleep(30);
    const third = client.heartbeat();
    pending.get(3)({ accepted: true, observed: { right: { on: false, sample: 10.18 } } });
    await third;
    await client.sleep(20);
    pending.get(1)({ accepted: true, observed: { right: { on: true, sample: 10 } } });
    await first;
    await client.sleep(150);
    pending.get(2)({ accepted: true, observed: { right: { on: true, sample: 10.15 } } });
    await second;
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, false);
    assert.equal(client.confirmed(RIGHT), false);
  } finally { await client.close(); }
});

test('native Off watermark survives counter reset even with newer HTTP sequences', async () => {
  let count = 0;
  const observations = [{ on: false, sample: 2 }, { on: true, sample: 1 },
    { on: true, sample: 1.1 }, { on: true, sample: 1.2 }];
  const { client, messages } = fixture({ request: async () => ({ accepted: true,
    observed: { right: observations[Math.min(count++, 3)] },
  }) });
  try {
    for (let i = 0; i < 4; i++) {
      await client.heartbeat();
      await client.sleep(150);
    }
    assert.ok(messages.every(message => message.states.right === false));
    assert.equal(client.confirmed(RIGHT), false);
  } finally { await client.close(); }
});

test('pending confirmation needs two new samples after a connection outage', async () => {
  let offline = false;
  const { client, messages } = fixture({ request: async device => {
    if (offline) throw Error('offline');
    return { accepted: true, observed: { right: { on: true, sample: device.now / 1000 } } };
  } });
  try {
    await client.heartbeat();
    offline = true;
    await assert.rejects(client.heartbeat());
    await client.sleep(10000);
    offline = false;
    await client.heartbeat();
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, false);
    await client.sleep(150);
    await client.heartbeat();
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, true);
  } finally { await client.close(); }
});

test('a response processed after the request deadline cannot count as current proof', async () => {
  let client;
  const f = fixture({ request: async () => {
    await client.sleep(1000);
    return { accepted: true, observed: { right: { on: true, sample: 10 } } };
  } });
  client = f.client;
  try {
    await assert.rejects(client.heartbeat(), /expired/i);
    assert.equal(client.confirmed(RIGHT), false);
  } finally { await client.close(); }
});

test('a restarted observer needs new proofs but its lower monotonic clock can recover', async () => {
  let epoch = 'a'.repeat(16);
  let sample = 100;
  const { client, messages } = fixture({ request: async () => ({ accepted: true,
    observer_epoch: epoch, observed: { right: { on: true, sample } },
  }) });
  try {
    await client.heartbeat();
    await client.sleep(10000);
    epoch = 'b'.repeat(16);
    sample = 1;
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, false);
    await client.sleep(150);
    sample = 2;
    await client.heartbeat();
    await client.heartbeat();
    assert.equal(messages.at(-1).states.right, true);
  } finally { await client.close(); }
});

test('power confirmation requires native samples newer than the pre-write sample', async () => {
  let count = 0;
  const { client } = fixture({ native: false, request: async () => ({ accepted: true,
    observed: { right: { on: true, sample: ++count <= 2 ? 100 : 101 } },
  }) });
  try {
    await assert.rejects(client.power(RIGHT, true, async () => {}, async () => {}), /confirm/i);
    assert.equal(client.confirmed(RIGHT), false);
  } finally { await client.close(); }
});

test('shutdown during verification cancels retries and cannot grant On', async () => {
  let entered;
  let release;
  const started = new Promise(resolve => { entered = resolve; });
  const held = new Promise(resolve => { release = resolve; });
  const { client, messages } = fixture({ native: false, sleep: async () => {
    entered();
    await held;
  } });
  let retries = 0;
  const task = client.power(RIGHT, true, async () => {}, async () => { retries++; });
  const rejected = assert.rejects(task, /closed/i);
  await started;
  await client.close();
  release();
  await rejected;
  assert.equal(retries, 0);
  assert.ok(messages.every(message => message.states.right === false));
});

test('heartbeat and verification requests never race the replay sequence on the wire', async () => {
  const releases = [];
  let active = 0;
  let peak = 0;
  const { client } = fixture({ request: async (_device, message) => {
    if (message.sequence > 2) return { accepted: true };
    active++;
    peak = Math.max(peak, active);
    await new Promise(resolve => releases.push(resolve));
    active--;
    return { accepted: true, observed: { right: { on: false, sample: message.sequence } } };
  } });
  const first = client.heartbeat();
  const second = client.heartbeat(true);
  try {
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(peak, 1);
    releases.shift()();
    await first;
    await new Promise(resolve => setImmediate(resolve));
    releases.shift()();
    await second;
    assert.equal(peak, 1);
  } finally {
    for (const release of releases) release();
    await Promise.allSettled([first, second]);
    await client.close();
  }
});

test('queued heartbeat samples the current Off mask and timestamp at dispatch', async () => {
  let release;
  const held = new Promise(resolve => { release = resolve; });
  const { client, messages } = fixture({ request: async (device, message) => {
    if (message.sequence === 1) await held;
    return { accepted: true, observed: { right: { on: false, sample: device.now / 1000 } } };
  } });
  client.states.set('right', true);
  const first = client.heartbeat();
  await new Promise(resolve => setImmediate(resolve));
  const queued = client.heartbeat();
  client.states.set('right', false);
  await client.sleep(200);
  release();
  try {
    await Promise.all([first, queued]);
    assert.equal(messages[0].states.right, true);
    assert.equal(messages[1].states.right, false);
    assert.ok(messages[1].issued_at > messages[0].issued_at);
  } finally { await client.close(); }
});

test('fresh native Off repairs a stale Home error without any power command', async () => {
  const { client, state } = fixture({ native: false });
  const updates = [];
  assert.equal(typeof client.observe, 'function');
  client.observe(RIGHT, value => updates.push(value));
  await client.heartbeat();
  assert.deepEqual(updates, [false]);
  state.native = true;
  await client.sleep(150);
  await client.heartbeat();
  assert.deepEqual(updates, [false]);
  await client.close();
});

test('status repair ignores unknown native power and waits for confirmed On', async () => {
  let unknown = true;
  const { client } = fixture({ request: async (device, message) => ({ accepted: true,
    observed: { right: unknown ? { on: null, sample: null }
      : { on: true, sample: device.now / 1000 } },
  }) });
  const updates = [];
  client.observe(RIGHT, value => updates.push(value));
  assert.equal(client.observe('unselected-third', () => {}), false);
  await client.heartbeat();
  assert.deepEqual(updates, []);
  unknown = false;
  await client.sleep(150);
  await client.heartbeat();
  assert.deepEqual(updates, []);
  await client.sleep(150);
  await client.heartbeat();
  assert.deepEqual(updates, [true]);
  await client.close();
  client.notifyObserved({ accepted: true, observed: { right: { on: true, sample: 1 } } });
  assert.deepEqual(updates, [true]);
});
