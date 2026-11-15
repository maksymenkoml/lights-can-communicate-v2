import assert from 'node:assert/strict';
import http from 'node:http';
import { PowerOwnerClient } from '../../experiments/homebridge-power/owner-client.mjs';

const base = process.argv[2];
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Loopback test endpoint required');
const right = 'D4:A5:D2:21:C0:46:4F:55';
const client = new PowerOwnerClient({ endpoint: base + '/power-owner',
  token: 'a'.repeat(48), confirmReadback: true });
client.register('19:FE:DA:C3:40:86:6A:7B', true);
client.register(right, true);

function fixturePower(on, drop) {
  return new Promise((resolve, reject) => {
    const request = http.request(base + '/test-power', { method: 'POST' }, response => {
      response.resume();
      response.on('end', () => response.statusCode === 200 ? resolve() : reject(Error('Fixture rejected')));
    });
    request.on('error', reject);
    request.end(JSON.stringify({ on, drop }));
  });
}

try {
  await client.heartbeat();
  await client.power(right, false, () => fixturePower(false, true), () => fixturePower(false, false));
  assert.equal(client.confirmed(right), true);
  await client.power(right, true, () => fixturePower(true, true), () => fixturePower(true, false));
  assert.equal(client.confirmed(right), true);
} finally { await client.close(); }
