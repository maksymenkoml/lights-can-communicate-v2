import path from 'node:path';
import { CoreClient, CREDENTIAL_FILE, readCredentials } from './client.js';
import { Lamp } from './lamp.js';

export const PLUGIN_NAME = 'homebridge-lights-can-communicate-v2';
export const PLATFORM_NAME = 'LightsCanCommunicateV2';
export const POLL_MS = 1000;
export const UNAVAILABLE_MS = 5000;
/**
 * Every lamp is addressed by its Govee device ID; the core resolves it through
 * its light registry. A `role` left over from an older configuration is ignored.
 */
export function validateLamps(lamps) {
  if (!Array.isArray(lamps) || lamps.length === 0) throw new Error('No lamps configured');
  const devices = new Set();
  for (const lamp of lamps) {
    if (!lamp || typeof lamp.deviceId !== 'string'
      || !/^([0-9A-F]{2}:){7}[0-9A-F]{2}$/.test(lamp.deviceId)
      || devices.has(lamp.deviceId) || typeof lamp.name !== 'string' || !lamp.name.trim()) {
      throw new Error('Invalid lamp configuration');
    }
    devices.add(lamp.deviceId);
  }
  return lamps;
}

export class LightsCanCommunicateV2Platform {
  constructor(log, config, api, { client, setInterval: every = setInterval, now = Date.now } = {}) {
    this.log = log;
    this.config = config ?? {};
    this.api = api;
    this.cached = new Map();
    this.lamps = new Map();
    this.now = now;
    this.every = every;
    this.lastState = now();
    this.client = client;
    this.polling = false;
    api.on('didFinishLaunching', () => this.start());
    api.on('shutdown', () => clearInterval(this.timer));
  }

  configureAccessory(accessory) {
    this.cached.set(accessory.UUID, accessory);
  }

  start() {
    try {
      const lamps = validateLamps(this.config.lamps);
      this.client ??= new CoreClient(readCredentials(
        path.join(this.api.user.storagePath(), CREDENTIAL_FILE)));
      const fresh = [];
      for (const lamp of lamps) {
        // The Govee plugin derives its UUID from the device ID; keep it identical.
        const uuid = this.api.hap.uuid.generate(lamp.deviceId);
        let accessory = this.cached.get(uuid);
        if (!accessory) {
          accessory = new this.api.platformAccessory(lamp.name, uuid);
          fresh.push(accessory);
        }
        this.cached.delete(uuid);
        this.lamps.set(lamp.deviceId, new Lamp(this.api, accessory, lamp, this.client, this.log));
      }
      if (fresh.length) this.api.registerPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, fresh);
      if (this.cached.size) {
        this.api.unregisterPlatformAccessories(PLUGIN_NAME, PLATFORM_NAME, [...this.cached.values()]);
        this.cached.clear();
      }
    } catch (error) {
      this.log.error(`Not started: ${error.message}`);
      return;
    }
    this.timer = this.every(() => this.poll(), POLL_MS);
    return this.poll();
  }

  async poll() {
    if (this.polling) return;
    this.polling = true;
    try {
      const lights = await this.client.state();
      this.lastState = this.now();
      for (const [deviceId, lamp] of this.lamps) lamp.update(lights[deviceId]);
    } catch (error) {
      if (this.now() - this.lastState >= UNAVAILABLE_MS) {
        this.log.warn(`Core unavailable: ${error.message}`);
        for (const lamp of this.lamps.values()) lamp.unavailable();
      }
    } finally {
      this.polling = false;
    }
  }
}
