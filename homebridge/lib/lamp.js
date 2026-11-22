/**
 * One desk lamp. The service layout mirrors the accessory that the Govee
 * plugin published (AccessoryInformation plus one Lightbulb without subtype,
 * with adaptive lighting), so HAP-NodeJS resolves the same AID and IIDs from
 * the bridge identifier cache and Apple Home keeps its rooms and scenes.
 */
export const BATCH_MS = 30;
export const NAME_MAX = 64; // HAP limit of the Name characteristic

export class Lamp {
  constructor(api, accessory, lamp, client, log) {
    this.hap = api.hap;
    this.accessory = accessory;
    this.role = lamp.role;
    this.client = client;
    this.log = log;
    this.pending = null;
    this.values = {};
    const { Service, Characteristic } = this.hap;
    accessory.context.role = lamp.role;
    accessory.context.deviceId = lamp.deviceId;

    const info = this.info = accessory.getService(Service.AccessoryInformation)
      || accessory.addService(Service.AccessoryInformation);
    info.setCharacteristic(Characteristic.Manufacturer, 'Govee')
      .setCharacteristic(Characteristic.Model, lamp.model || 'H6076')
      .setCharacteristic(Characteristic.SerialNumber, lamp.deviceId);
    // The Govee plugin adds ConfiguredName here when it creates an accessory;
    // without it the cached IID would expire. Home writes it on renames, so an
    // existing value is kept.
    if (!info.testCharacteristic(Characteristic.ConfiguredName)) {
      info.setCharacteristic(Characteristic.ConfiguredName, lamp.name);
    }

    this.service = accessory.getService(Service.Lightbulb) || accessory.addService(Service.Lightbulb);
    const characteristic = (type) => this.service.getCharacteristic(type);
    characteristic(Characteristic.On).onSet(value => this.send({ on: value === true }));
    characteristic(Characteristic.Brightness)
      .onSet(value => this.send({ brightness: Math.round(value) }));
    characteristic(Characteristic.Hue).onSet(value => this.send({ hue: value }, true));
    characteristic(Characteristic.Saturation).onSet(value => this.send({ saturation: value }, true));
    characteristic(Characteristic.ColorTemperature)
      .onSet(value => this.send({ color_temperature: Math.round(value) }));

    accessory.configureController(new this.hap.AdaptiveLightingController(this.service, {
      controllerMode: this.hap.AdaptiveLightingControllerMode.AUTOMATIC,
    }));
  }

  /** Hue and saturation arrive as separate writes; send them as one color. */
  send(fields, color = false) {
    if (!color) return this.forward(fields);
    if (!this.pending) {
      const pending = { fields: {} };
      pending.promise = new Promise(resolve => setTimeout(resolve, BATCH_MS)).then(() => {
        this.pending = null;
        const merged = {
          hue: this.values.hue ?? 0, saturation: this.values.saturation ?? 0, ...pending.fields,
        };
        return this.forward(merged);
      });
      this.pending = pending;
    }
    Object.assign(this.pending.fields, fields);
    return this.pending.promise;
  }

  async forward(fields) {
    try {
      const roles = await this.client.set({ role: this.role, ...fields });
      this.update(roles?.[this.role]);
    } catch (error) {
      this.log.warn(`${this.accessory.displayName}: ${error.message}`);
      throw new this.hap.HapStatusError(this.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE);
    }
  }

  /**
   * Show the owner's name from Light Studio. Only display names change: UUID,
   * AID and IID stay, so rooms, scenes and automations survive. The
   * AccessoryInformation Name and ConfiguredName follow; Name and
   * ConfiguredName on the Lightbulb are updated only where the accessory
   * already has them, so no characteristic (and no IID) is added. Apple Home
   * may keep a name that the owner edited in the Home app.
   */
  rename(name) {
    const value = typeof name === 'string' ? name.trim() : '';
    if (!value || value.length > NAME_MAX || value === this.appliedName) return;
    this.appliedName = value;
    const { Characteristic } = this.hap;
    if (typeof this.accessory.updateDisplayName === 'function') {
      this.accessory.updateDisplayName(value);
    } else {
      this.accessory.displayName = value;
      this.accessory._associatedHAPAccessory.displayName = value;
    }
    this.info.updateCharacteristic(Characteristic.Name, value);
    this.info.updateCharacteristic(Characteristic.ConfiguredName, value);
    for (const type of [Characteristic.Name, Characteristic.ConfiguredName]) {
      if (type && this.service.testCharacteristic(type)) this.service.updateCharacteristic(type, value);
    }
    this.log.info?.(`Name from Light Studio: ${value}`);
  }

  /** Apply the core's state; unknown values keep the last known HomeKit value. */
  update(state) {
    if (!state || typeof state !== 'object') return;
    const { Characteristic } = this.hap;
    if (state.name !== undefined) this.rename(state.name);
    const apply = (type, key, value) => {
      if (value === null || value === undefined || this.values[key] === value) return;
      this.values[key] = value;
      this.service.updateCharacteristic(type, value);
    };
    if (typeof state.on === 'boolean') apply(Characteristic.On, 'on', state.on);
    if (Number.isInteger(state.brightness)) {
      apply(Characteristic.Brightness, 'brightness', state.brightness);
    }
    if (typeof state.hue === 'number') apply(Characteristic.Hue, 'hue', state.hue);
    if (typeof state.saturation === 'number') {
      apply(Characteristic.Saturation, 'saturation', state.saturation);
    }
    if (Number.isInteger(state.color_temperature)) {
      apply(Characteristic.ColorTemperature, 'color_temperature', state.color_temperature);
    }
  }

  unavailable() {
    this.values = {};
    this.service.updateCharacteristic(
      this.hap.Characteristic.On,
      new this.hap.HapStatusError(this.hap.HAPStatus.SERVICE_COMMUNICATION_FAILURE),
    );
  }
}
