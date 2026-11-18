import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { PowerOwnerClient } from '../../homebridge-power/owner-client.mjs';
import { coordinatedNativeWrites } from '../../homebridge-power/hook.mjs';

const base = process.argv[2];
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Loopback endpoint required');
const ids = { left: '19:FE:DA:C3:40:86:6A:7B', right: 'D4:A5:D2:21:C0:46:4F:55' };
const client = new PowerOwnerClient({ endpoint: base + '/power-owner',
  token: 'a'.repeat(48), confirmReadback: true });
const updates = [];
const setters = {};
async function post(path, body, token) {
  const response = await fetch(base + path, { method: 'POST', signal: AbortSignal.timeout(14000),
    headers: { 'Content-Type': 'application/json', ...(token ? { 'X-Adapter-Token': token } : {}) },
    body: JSON.stringify(body) });
  return { status: response.status, body: await response.json() };
}
for (const [role, id] of Object.entries(ids)) {
  client.register(id, false);
  const control = { accessory: { context: { gvDeviceId: id } }, hapChar: { On: 'on' },
    service: { updateCharacteristic: (key, value) => updates.push([role, key, value]) },
    platform: { sendDeviceUpdate: async (_accessory, message) => {
      await post('/test-power', { role, on: message.value === 'on' });
    } } };
  setters[role] = coordinatedNativeWrites(control, id, client, async on => {
    const response = await post('/test-power', { role, on });
    assert.equal(response.status, 200);
  });
}
async function hue(states) {
  const response = await post('/test-hue', {
    id: randomUUID().replaceAll('-', ''), issued_at: Date.now() / 1000, states,
  }, 'adapter-key');
  assert.equal(response.status, 200, JSON.stringify(response.body));
  assert.deepEqual(response.body, { accepted: true });
}
try {
  await client.heartbeat();
  await client.heartbeat(); // Negotiate support before advertising handlers.
  client.start();
  await hue({ right: true });
  assert.equal(client.confirmed(ids.right), true);
  await hue({ right: false });
  await hue({ left: true, right: true });
  await setters.right(false); // Existing Home path remains authoritative.
  assert.equal(client.desired.get('right'), false);
  assert.ok(updates.some(([role, key, on]) => role === 'right' && key === 'on' && on === false));
} finally {
  await client.close();
}
