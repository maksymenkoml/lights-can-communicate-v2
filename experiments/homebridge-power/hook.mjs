import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { DEVICE_ROLES, PowerOwnerClient } from './owner-client.mjs';

const clients = new Map();

export function coordinatedSetter(control, original) {
  const id = String(control.accessory.context.gvDeviceId).toUpperCase();
  if (!DEVICE_ROLES[id]) return original;
  let client;
  try {
    const storage = control.platform.api.user.storagePath();
    const configPath = path.join(storage, 'smart-lighting-power.json');
    if (!existsSync(configPath)) return original;
    client = clients.get(storage);
    if (!client) {
      const config = JSON.parse(readFileSync(configPath, 'utf8'));
      if (config.version !== 1 || Object.keys(config).some(key =>
        !['version', 'endpoint', 'token'].includes(key))) throw Error('Invalid owner config');
      client = new PowerOwnerClient({ ...config,
        statePath: path.join(storage, 'smart-lighting-power-state.json') });
      clients.set(storage, client);
      control.platform.api.on('shutdown', async () => {
        await client.close();
        clients.delete(storage);
      });
    }
    client.register(id, control.service.getCharacteristic(control.hapChar.On).value);
    client.start();
  } catch {
    if (client) void client.close();
    control.accessory.logWarn('Smart Lighting power coordination unavailable; original Home control retained.');
    return original;
  }
  return value => client.power(id, value, () => original(value));
}
