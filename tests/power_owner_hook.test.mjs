import assert from 'node:assert/strict';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { coordinatedSetter } from '../experiments/homebridge-power/hook.mjs';

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
  const server = http.createServer((request, response) => {
    assert.equal(request.url, '/power-owner');
    assert.equal(request.headers['x-power-owner-token'], 'b'.repeat(48));
    let body = '';
    request.on('data', chunk => { body += chunk; });
    request.on('end', () => {
      messages.push(JSON.parse(body));
      response.writeHead(200, { 'Content-Type': 'application/json' });
      response.end('{"accepted":true}');
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
      if (!value) assert.equal(messages.at(-1).states.right, false);
      return 'result';
    });
    assert.equal(await setter(true), 'result');
    assert.equal(messages.at(-1).states.right, true);
    assert.equal(await setter(false), 'result');
    assert.deepEqual(values, [true, false]);
    assert.equal(device.shutdown.length, 1);
  } finally {
    if (device) await Promise.all(device.shutdown.map(callback => callback()));
    await new Promise(resolve => server.close(resolve));
    rmSync(folder, { recursive: true });
  }
});
