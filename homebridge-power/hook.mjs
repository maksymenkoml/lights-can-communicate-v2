import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { AsyncLocalStorage } from 'node:async_hooks';
import { setImmediate as nextTurn } from 'node:timers/promises';
import { DEVICE_ROLES, PowerOwnerClient } from './owner-client.mjs';

const clients = new Map();
const powerWriteContext = new AsyncLocalStorage();
const guardedPlatforms = new WeakSet();
const guardedTransports = new WeakSet();
const guardedQueues = new WeakSet();
const guardedBleWrites = new WeakSet();

function checkPowerWrite(accessory) {
  const remote = powerWriteContext.getStore();
  if (remote && accessory === remote.accessory) {
    remote.guard();
    remote.assertIdentity();
  }
}

function guardTransportWrites(platform) {
  for (const name of ['lanClient', 'awsClient', 'openApiClient', 'bleClient']) {
    const transport = platform[name];
    if (!transport || typeof transport.updateDevice !== 'function'
      || guardedTransports.has(transport)) continue;
    const update = transport.updateDevice;
    transport.updateDevice = function (accessory, ...args) {
      // A failed route can await before the SDK chooses its next transport.
      checkPowerWrite(accessory);
      const remote = powerWriteContext.getStore();
      const selected = remote?.accessory === accessory ? remote : undefined;
      // Lower BLE writes lack an accessory argument. Clear a different device's
      // request context before entering this transport's asynchronous operation.
      return powerWriteContext.run(selected, () => update.call(this, accessory, ...args));
    };
    guardedTransports.add(transport);
  }
  const ble = platform.bleClient;
  if (ble && typeof ble.writeWithTimeout === 'function' && !guardedBleWrites.has(ble)) {
    const write = ble.writeWithTimeout;
    ble.writeWithTimeout = function (...args) {
      const remote = powerWriteContext.getStore();
      // Connection, discovery and encryption can all await after updateDevice.
      // The pinned SDK calls characteristic.writeAsync immediately in this method.
      remote?.guard();
      remote?.assertIdentity();
      return write.apply(this, args);
    };
    guardedBleWrites.add(ble);
  }
  const queue = platform.queue;
  if (!queue || typeof queue.add !== 'function' || guardedQueues.has(queue)) return;
  const add = queue.add;
  queue.add = function (task, ...args) {
    const remote = powerWriteContext.getStore();
    // External queues may invoke callbacks in the previous task's context.
    const queued = typeof task === 'function' ? function (...taskArgs) {
      return powerWriteContext.run(remote, () => task.apply(this, taskArgs));
    } : task;
    return add.call(this, queued, ...args);
  };
  guardedQueues.add(queue);
}

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
  if (typeof control.platform.sendDeviceUpdate === 'function'
    && !guardedPlatforms.has(control.platform)) {
    guardTransportWrites(control.platform);
    const send = control.platform.sendDeviceUpdate;
    control.platform.sendDeviceUpdate = function (accessory, ...args) {
      // The SDK can debounce inside the original setter. Recheck at the sender,
      // while limiting this request's guard to its selected accessory.
      checkPowerWrite(accessory);
      guardTransportWrites(this);
      return send.call(this, accessory, ...args);
    };
    guardedPlatforms.add(control.platform);
  }
  client.observe?.(id, value => {
    if (powerOperations) return;
    assertIdentity();
    control.cacheState = value ? 'on' : 'off';
    control.service.updateCharacteristic?.(control.hapChar.On, value);
  });

  const power = (value, guard) => {
    guard?.();
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
    const remote = guard ? { accessory: control.accessory, guard, assertIdentity } : undefined;
    pendingPower = client.power(id, value, async () => {
      await Promise.allSettled(drain);
      guard?.();
      assertIdentity();
      return powerWriteContext.run(remote, () => original(value));
    }, () => {
      guard?.();
      assertIdentity();
      return powerWriteContext.run(remote, () => control.platform.sendDeviceUpdate(control.accessory, {
        cmd: 'state', value: value ? 'on' : 'off',
      }));
    }, guard).then(result => {
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
            control.accessory.log?.(`Lights Can Communicate v2 suppressed ${name} while power intent is Off.`);
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
  client.registerPowerHandler?.(id, power);
  return power;
}

export function coordinatedSetter(control, original) {
  const id = String(control.accessory.context.gvDeviceId).toUpperCase();
  if (!DEVICE_ROLES[id]) return original;
  let client;
  try {
    const storage = control.platform.api.user.storagePath();
    const configPath = path.join(storage, 'lights-can-communicate-v2-power.json');
    if (!existsSync(configPath)) return original;
    client = clients.get(storage);
    if (!client) {
      const config = JSON.parse(readFileSync(configPath, 'utf8'));
      if (config.version !== 1 || Object.keys(config).some(key =>
        !['version', 'endpoint', 'token'].includes(key))) throw Error('Invalid owner config');
      client = new PowerOwnerClient({ ...config,
        statePath: path.join(storage, 'lights-can-communicate-v2-power-state.json'),
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
    control.accessory.logWarn('Lights Can Communicate v2 power coordination unavailable; original Home control retained.');
    return original;
  }
  return coordinatedNativeWrites(control, id, client, original);
}
