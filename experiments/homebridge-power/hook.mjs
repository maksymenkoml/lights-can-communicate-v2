import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { AsyncLocalStorage } from 'node:async_hooks';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { DEVICE_ROLES, PowerOwnerClient } from './owner-client.mjs';

const clients = new Map();

export function coordinatedNativeWrites(control, id, client, original) {
  id = String(id).toUpperCase();
  const role = DEVICE_ROLES[id];
  if (!role) return original;
  let intent = (client.desired?.get(role) ?? client.states.get(role)) === true;
  let epoch = 0;
  let pendingPower = Promise.resolve();
  let independentColor = false;
  let powerOperations = 0;
  const activeWrites = new Set();
  const suppressed = new Set();
  const writerContext = new AsyncLocalStorage();
  const services = new Set([control.service, ...(control.usedCodes ?? [])
    .map(name => control.accessory.getService?.(name)).filter(Boolean)]);
  for (const service of services) {
    if (typeof service.updateCharacteristic !== 'function') continue;
    const update = service.updateCharacteristic.bind(service);
    service.updateCharacteristic = (...args) => {
      const writerEpoch = writerContext.getStore();
      // SDK timers inherit this context after the native writer promise settles.
      if (writerEpoch !== undefined && (writerEpoch !== epoch || !intent
        || client.closed || client.closing
        || String(control.accessory.context.gvDeviceId).toUpperCase() !== id)) return service;
      return update(...args);
    };
  }
  const assertIdentity = () => {
    if (String(control.accessory.context.gvDeviceId).toUpperCase() !== id) {
      throw Error('Selected power accessory identity changed');
    }
  };
  client.observe?.(id, value => {
    if (powerOperations) return;
    assertIdentity();
    control.cacheState = value ? 'on' : 'off';
    control.service.updateCharacteristic?.(control.hapChar.On, value);
  });

  const power = value => {
    assertIdentity();
    if (typeof value !== 'boolean') return Promise.reject(Error('Expected owner power boolean'));
    const requestedEpoch = ++epoch;
    powerOperations++;
    intent = value;
    independentColor = false;
    suppressed.clear();
    if (!value) {
      // Original SDK writers check these keys after their debounce delays.
      for (const key of ['updateKeyBright', 'updateKeyColour', 'updateKeyCT']) control[key] = Symbol('off');
    }
    const drain = value ? [] : [...activeWrites];
    pendingPower = client.power(id, value, async () => {
      await Promise.allSettled(drain);
      assertIdentity();
      return original(value);
    }, () => {
      assertIdentity();
      return control.platform.sendDeviceUpdate(control.accessory, {
        cmd: 'state', value: value ? 'on' : 'off',
      });
    }).then(result => {
      assertIdentity();
      if (client.confirmed?.(id)) control.cacheState = value ? 'on' : 'off';
      if (requestedEpoch === epoch) control.service.updateCharacteristic?.(control.hapChar.On, value);
      if (requestedEpoch === epoch && value) independentColor = true;
      return result;
    }).catch(error => {
      if (requestedEpoch === epoch && value) intent = false;
      throw error;
    }).finally(() => { powerOperations--; });
    return pendingPower;
  };

  for (const name of ['internalBrightnessUpdate', 'internalColourUpdate', 'internalCTUpdate',
    'internalSceneUpdate']) {
    if (typeof control[name] !== 'function') continue;
    const write = control[name].bind(control);
    control[name] = (...args) => {
      const enteredEpoch = epoch;
      const zeroBrightness = name === 'internalBrightnessUpdate' && args[0] === 0;
      const task = (async () => {
        // HomeKit may deliver color/brightness before the paired On write.
        await nextTurn();
        if (client.closed || client.closing) return;
        if (epoch !== enteredEpoch && !(epoch === enteredEpoch + 1 && intent)) return;
        if (zeroBrightness) {
          if (epoch === enteredEpoch) await power(false);
          return;
        }
        const explicitOn = (name === 'internalSceneUpdate' && args[3] === true)
          || (name === 'internalColourUpdate' && args[1] === true);
        if (explicitOn && epoch === enteredEpoch && !intent) {
          const on = power(true);
          const implicitEpoch = epoch;
          await on;
          if (epoch !== implicitEpoch || !intent) return;
        }
        if (!intent) {
          if (!suppressed.has(name)) {
            control.accessory.log?.(`Smart Lighting suppressed ${name} while power intent is Off.`);
            suppressed.add(name);
          }
          return;
        }
        const selectedEpoch = epoch;
        try { await pendingPower; } catch { return; }
        if (!intent || epoch !== selectedEpoch || client.closed || client.closing) return;
        if (!client.confirmed?.(id) && !independentColor) return;
        assertIdentity();
        const native = Promise.resolve().then(() => {
          if (intent && epoch === selectedEpoch && !client.closed && !client.closing) {
            return writerContext.run(selectedEpoch, () => write(...args));
          }
        });
        activeWrites.add(native);
        try { return await native; } finally { activeWrites.delete(native); }
      })();
      return task.finally(() => {
        if (zeroBrightness) {
          // As in the original SDK, retain the previous nonzero Home brightness.
          void nextTurn().then(() => {
            if (client.closed || client.closing
              || String(control.accessory.context.gvDeviceId).toUpperCase() !== id) return;
            if (Number.isFinite(control.cacheBright) && control.cacheBright > 0
              && control.hapChar.Brightness) {
              control.service.updateCharacteristic?.(control.hapChar.Brightness, control.cacheBright);
            }
          }).catch(() => {});
        }
      });
    };
  }
  return power;
}

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
        statePath: path.join(storage, 'smart-lighting-power-state.json'),
        confirmReadback: true,
      }, { warn: message => control.accessory.logWarn(message) });
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
  return coordinatedNativeWrites(control, id, client, original);
}
