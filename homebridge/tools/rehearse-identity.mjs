#!/usr/bin/env node
// Read-only migration rehearsal: does the plugin resolve the cached HomeKit IDs?
//
// node homebridge/tools/rehearse-identity.mjs <IdentifierCache.json copy> <lamps.json>
//
// lamps.json holds the planned `lamps` array of the platform configuration.
// The cache copy is only read; nothing is written or published.
import { readFileSync } from 'node:fs';
import * as hap from '../node_modules/@homebridge/hap-nodejs/dist/index.js';
import { IdentifierCache } from '../node_modules/@homebridge/hap-nodejs/dist/lib/model/IdentifierCache.js';
import { Lamp } from '../lib/lamp.js';
import { validateLamps } from '../lib/platform.js';

class Accessory {
  constructor(displayName, UUID) {
    this._associatedHAPAccessory = new hap.Accessory(displayName, UUID);
    this.displayName = displayName;
    this.UUID = UUID;
    this.context = {};
  }

  getService(type) { return this._associatedHAPAccessory.getService(type); }
  addService(...args) { return this._associatedHAPAccessory.addService(...args); }
  configureController(controller) { this._associatedHAPAccessory.configureController(controller); }
}

export function rehearse(saved, lamps) {
  validateLamps(lamps);
  const cache = new IdentifierCache('rehearsal');
  cache._cache = JSON.parse(JSON.stringify(saved));
  const bridge = new hap.Bridge('rehearsal', hap.uuid.generate('rehearsal'));
  const api = { hap };
  const accessories = lamps.map((lamp) => {
    const accessory = new Accessory(lamp.name, hap.uuid.generate(lamp.deviceId));
    new Lamp(api, accessory, lamp, null, console);
    bridge.addBridgedAccessory(accessory._associatedHAPAccessory);
    return accessory;
  });
  cache.startTrackingUsage();
  bridge._assignIDs(cache);
  const used = cache._usedCache;
  return accessories.map((accessory) => {
    const prefix = accessory.UUID;
    const own = key => key === prefix || key.startsWith(`${prefix}|`);
    const ignored = key => key.endsWith('|nextIID');
    return {
      name: accessory.displayName,
      known: prefix in saved,
      aid: accessory._associatedHAPAccessory.aid,
      added: Object.keys(used).filter(key => own(key) && !ignored(key) && !(key in saved)),
      unused: Object.keys(saved).filter(key => own(key) && !ignored(key) && !(key in used)),
    };
  });
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const [cacheFile, lampsFile] = process.argv.slice(2);
  if (!cacheFile || !lampsFile) {
    console.error('Usage: rehearse-identity.mjs <IdentifierCache.json copy> <lamps.json>');
    process.exit(2);
  }
  const saved = JSON.parse(readFileSync(cacheFile, 'utf8')).cache;
  const report = rehearse(saved, JSON.parse(readFileSync(lampsFile, 'utf8')));
  console.log(JSON.stringify(report, null, 2));
  process.exit(report.every(item => item.known && !item.added.length && !item.unused.length) ? 0 : 1);
}
