import http from 'node:http';
import { PowerOwnerClient } from '../../homebridge-power/owner-client.mjs';

const base = process.argv[2];
if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(base)) throw Error('Loopback test endpoint required');
const client = new PowerOwnerClient({ endpoint: base + '/power-owner', token: 'a'.repeat(48) });
const right = 'D4:A5:D2:21:C0:46:4F:55';
client.register('19:FE:DA:C3:40:86:6A:7B', true);
client.register(right, true);

function testOperation(route, body = {}) {
  return new Promise((resolve, reject) => {
    const request = http.request(base + route, { method: 'POST' }, response => {
      response.resume();
      response.on('end', () => response.statusCode === 200 ? resolve() : reject(Error('Fixture rejected')));
    });
    request.on('error', reject);
    request.end(JSON.stringify(body));
  });
}

try {
  await client.heartbeat();
  await client.power(right, false, () => testOperation('/test-power', { on: false }));
  await testOperation('/test-render');
  await client.power(right, true, () => testOperation('/test-power', { on: true }));
  await testOperation('/test-render');
} finally { await client.close(); }
