import { randomUUID } from 'node:crypto';
import { existsSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import http from 'node:http';
import { isIPv4 } from 'node:net';
import { setTimeout as delay } from 'node:timers/promises';

export const DEVICE_ROLES = Object.freeze({
  '19:FE:DA:C3:40:86:6A:7B': 'left',
  'D4:A5:D2:21:C0:46:4F:55': 'right',
});
const SAFE_GRANT_EXPIRY_MS = 2000 + 500 + 400;
class IntentRejected extends Error {}

function powerObservation(response, role) {
  const observed = response?.observed?.[role];
  if (!observed || !((observed.on === null && observed.sample === null)
    || (typeof observed.on === 'boolean' && typeof observed.sample === 'number'
      && Number.isFinite(observed.sample) && observed.sample >= 0))) {
    throw new IntentRejected('Power owner readback unavailable');
  }
  const epoch = response.observer_epoch ?? 'legacy';
  if (epoch !== 'legacy' && !/^[0-9a-f]{16}$/.test(epoch)) {
    throw new IntentRejected('Power observer identity unavailable');
  }
  return { ...observed, epoch };
}

function resetSamples(pending) {
  pending.samples = 0;
  pending.since = undefined;
  pending.lastAt = undefined;
}

function endpoint(value) {
  const url = new URL(value);
  const bytes = url.hostname.split('.').map(Number);
  const local = bytes[0] === 127 || bytes[0] === 10
    || (bytes[0] === 192 && bytes[1] === 168)
    || (bytes[0] === 172 && bytes[1] >= 16 && bytes[1] <= 31);
  if (url.protocol !== 'http:' || !isIPv4(url.hostname) || !local
    || url.username || url.password || url.pathname !== '/power-owner' || url.search || url.hash) {
    throw Error('Invalid local power owner endpoint');
  }
  return url;
}

function post(url, token, message) {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify(message);
    let finished = false;
    let responseStarted = false;
    let deadline;
    const done = (error, result) => {
      if (finished) return;
      finished = true;
      clearTimeout(deadline);
      if (error) reject(error instanceof IntentRejected ? error : responseStarted
        ? new IntentRejected('Power owner rejected request') : Error('Power owner unavailable'));
      else resolve(result);
    };
    const request = http.request(url, { method: 'POST', headers: {
      'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body),
      'X-Power-Owner-Token': token,
    } }, response => {
      responseStarted = true;
      let data = '';
      response.on('data', chunk => {
        data += chunk;
        if (Buffer.byteLength(data) > 4096) request.destroy(Error('Oversized owner response'));
      });
      response.on('error', done);
      response.on('end', () => {
        try {
          if (response.statusCode !== 200) throw new IntentRejected('Power owner rejected request');
          done(null, JSON.parse(data));
        } catch (error) { done(error); }
      });
    });
    deadline = setTimeout(() => request.destroy(Error('Owner timeout')), 350);
    deadline.unref();
    request.on('error', done);
    request.end(body);
  });
}

export class PowerOwnerClient {
  constructor(config, dependencies = {}) {
    this.url = endpoint(config.endpoint);
    if (!/^[0-9a-f]{32,128}$/.test(config.token || '')) throw Error('Invalid owner credential');
    this.request = dependencies.request || (message => post(this.url, config.token, message));
    this.clock = dependencies.clock || (() => performance.now());
    this.wallClock = dependencies.wallClock || Date.now;
    this.sleep = dependencies.sleep || delay;
    this.warn = dependencies.warn || (() => {});
    this.confirmReadback = config.confirmReadback === true;
    this.confirmedRoles = new Map();
    this.observers = new Map();
    this.startedAt = this.clock();
    this.statePath = config.statePath;
    this.persisted = {};
    if (this.statePath && existsSync(this.statePath)) {
      try {
        const state = JSON.parse(readFileSync(this.statePath, 'utf8'));
        if (!state || typeof state !== 'object' || Array.isArray(state)
          || !Object.keys(state).length || Object.keys(state).some(role =>
          !['left', 'right'].includes(role) || typeof state[role] !== 'boolean')) throw Error();
        this.persisted = { left: false, right: false, ...state };
      } catch { this.persisted = { left: false, right: false }; }
    }
    this.states = new Map();
    this.desired = new Map();
    this.pendingOn = new Map();
    this.queues = new Map();
    this.lastGrant = new Map();
    this.session = randomUUID();
    this.sequence = 0;
    this.lastRejection = -1;
    this.lastAcknowledgement = -1;
    this.closed = false;
    this.closing = false;
    this.heartbeatBusy = false;
  }

  register(id, currentOn) {
    const role = DEVICE_ROLES[String(id).toUpperCase()];
    if (!role) return false;
    if (!this.states.has(role)) {
      const wanted = this.persisted[role] ?? currentOn === true;
      this.desired.set(role, wanted);
      this.states.set(role, this.confirmReadback ? false : wanted);
      if (this.confirmReadback && wanted) this.pendingOn.set(role, { samples: 0 });
    }
    return true;
  }

  persist() {
    if (!this.statePath) return;
    Object.assign(this.persisted, Object.fromEntries(this.confirmReadback ? this.desired : this.states));
    const temporary = `${this.statePath}.tmp-${randomUUID()}`;
    try {
      writeFileSync(temporary, JSON.stringify(this.persisted), { mode: 0o600, flag: 'wx' });
      renameSync(temporary, this.statePath);
    } finally {
      rmSync(temporary, { force: true });
    }
  }

  async send(states, closing = false, observations = false) {
    if (this.closed || (this.closing && !closing) || !Object.keys(states).length) return false;
    const message = { session: this.session, sequence: ++this.sequence,
      issued_at: this.wallClock() / 1000, states };
    const started = this.clock();
    for (const [role, on] of Object.entries(states)) {
      if (on) this.lastGrant.set(role, this.clock());
    }
    try {
      const result = await this.request(message);
      if (this.clock() - started > 350) throw new IntentRejected('Expired power owner response');
      if (!result || result.accepted !== true) throw new IntentRejected('Power owner rejected intent');
      const current = message.sequence > this.lastAcknowledgement;
      this.lastAcknowledgement = Math.max(this.lastAcknowledgement, message.sequence);
      if (current) {
        this.recoverPending(result);
        this.notifyObserved(result);
      }
      else {
        for (const [role, pending] of this.pendingOn) {
          let observed;
          try { observed = powerObservation(result, role); } catch { continue; }
          if (observed.on === false && observed.epoch === pending.epoch
            && (pending.sample === undefined || observed.sample > pending.sample)) {
            pending.sample = observed.sample;
            resetSamples(pending);
          }
        }
      }
      if (!observations) return true;
      return current ? result : { ...result, observed: Object.fromEntries(
        Object.keys(states).map(role => [role, { on: null, sample: null }]),
      ) };
    } catch (error) {
      for (const pending of this.pendingOn.values()) resetSamples(pending);
      if (error instanceof IntentRejected) {
        this.lastRejection = Math.max(this.lastRejection, message.sequence);
      }
      throw error;
    }
  }

  heartbeat(observations = false) {
    // Timer and verification callers share one wire order. Allocate timestamps,
    // sequence and the latest permission mask only when the queued send starts.
    const previous = this.heartbeatQueue || Promise.resolve();
    const task = previous.catch(() => {}).then(() =>
      this.send(Object.fromEntries(this.states), false, observations));
    this.heartbeatQueue = task;
    return task;
  }

  confirmed(id) {
    return this.confirmedRoles.get(DEVICE_ROLES[String(id).toUpperCase()]) === true;
  }

  observe(id, listener) {
    const role = DEVICE_ROLES[String(id).toUpperCase()];
    if (!role || typeof listener !== 'function') return false;
    this.observers.set(role, listener);
    return true;
  }

  notifyObserved(response) {
    if (!this.confirmReadback || this.closed || this.closing) return;
    for (const [role, listener] of this.observers) {
      let observed;
      try { observed = powerObservation(response, role); } catch { continue; }
      const wanted = this.desired.get(role);
      if (observed.on === null || observed.on !== wanted
        || (wanted && this.confirmedRoles.get(role) !== true)) continue;
      // Repair stale HAP communication errors from matching fresh telemetry;
      // this never sends power or promotes an unconfirmed On into a color grant.
      try { listener(observed.on); } catch { /* Keep the wire acknowledgement valid. */ }
    }
  }

  recoverPending(response) {
    if (this.closed || this.closing) return;
    for (const [role, pending] of this.pendingOn) {
      if (this.desired.get(role) !== true) continue;
      let observed;
      try { observed = powerObservation(response, role); }
      catch { resetSamples(pending); continue; }
      const now = this.clock();
      if (pending.lastAt !== undefined && now - pending.lastAt > 750) resetSamples(pending);
      if (observed.epoch !== pending.epoch) {
        resetSamples(pending);
        pending.epoch = observed.epoch;
        pending.sample = undefined;
      }
      if (observed.on === null) {
        resetSamples(pending);
        continue;
      }
      if (pending.sample !== undefined && observed.sample <= pending.sample) continue;
      pending.sample = observed.sample;
      if (!observed.on) {
        resetSamples(pending);
        continue;
      }
      pending.samples++;
      pending.lastAt = now;
      pending.since ??= now;
      if (pending.samples >= 2 && now - pending.since >= 150) {
        this.persist();
        this.states.set(role, true);
        this.confirmedRoles.set(role, true);
        this.pendingOn.delete(role);
      }
    }
  }

  async verifyPower(role, value, retry, baseline) {
    const overallStarted = this.clock();
    let minimumSample = baseline?.sample ?? undefined;
    let epoch = baseline?.epoch;
    let observedOpposite = false;
    for (let attempt = 0; attempt < 3; attempt++) {
      const started = this.clock();
      // The measured H6076 Off telemetry can converge about five seconds after
      // a zonal stream stops. Do not classify that transition as a lost write.
      const firstOff = !value && attempt === 0;
      const retryAfter = firstOff ? 6000 : 450;
      const window = firstOff ? 7500 : 2400;
      let stableSince;
      let previousSample;
      let samples = 0;
      let retried = false;
      while (this.clock() - started <= window && this.clock() - overallStarted <= 8500) {
        if (this.closed || this.closing) throw Error('Power owner closed');
        let response;
        try { response = await this.heartbeat(true); }
        catch (error) {
          if (error instanceof IntentRejected) throw error;
          if (observedOpposite) throw Error('Device power could not be confirmed');
          this.warn(`Smart Lighting ${role} power unconfirmed: coordinator unavailable; original Home control retained.`);
          return false;
        }
        if (this.closed || this.closing) throw Error('Power owner closed');
        const observed = powerObservation(response, role);
        if (observed.epoch !== epoch) {
          epoch = observed.epoch;
          minimumSample = undefined;
          stableSince = previousSample = undefined;
          samples = 0;
        }
        const fresh = observed.on !== null
          && (minimumSample === undefined || observed.sample > minimumSample);
        const now = this.clock();
        if (fresh && observed.on === value) {
          stableSince ??= now;
          if (previousSample === undefined || observed.sample > previousSample) {
            previousSample = observed.sample;
            samples++;
          }
          if (samples >= 2 && (value ? now - started >= 150
            : now - started >= 1500 && now - stableSince >= 1200)) return true;
        } else {
          stableSince = undefined;
          previousSample = undefined;
          samples = 0;
          // An unknown/stale identity never authorizes another physical write.
          if (fresh) observedOpposite = true;
          if (fresh && now - started >= retryAfter) {
            if (attempt === 2 || typeof retry !== 'function') {
              throw Error('Device power could not be confirmed');
            }
            if (this.closed || this.closing) throw Error('Power owner closed');
            minimumSample = observed.sample;
            await retry();
            retried = true;
            break;
          }
        }
        await this.sleep(150);
      }
      if (!retried) throw Error('Device power could not be confirmed');
    }
    throw Error('Device power could not be confirmed');
  }

  start() {
    if (this.timer || this.closed || this.closing) return;
    const tick = async () => {
      if (this.heartbeatBusy) return;
      this.heartbeatBusy = true;
      try { await this.heartbeat(); } catch { /* Grants expire while disconnected. */ }
      finally { this.heartbeatBusy = false; }
    };
    this.timer = setInterval(tick, 350);
    this.timer.unref();
    void tick();
  }

  async pauseBeforeOff(role, observations = false) {
    const deadline = Math.max(this.startedAt, this.lastGrant.get(role) ?? -Infinity)
      + SAFE_GRANT_EXPIRY_MS;
    let rejected = this.lastRejection > this.lastAcknowledgement;
    while (true) {
      if (this.closed || this.closing) throw Error('Power owner closed');
      try { return await this.heartbeat(observations); }
      catch (error) {
        const remaining = deadline - this.clock();
        rejected ||= error instanceof IntentRejected
          || this.lastRejection > this.lastAcknowledgement;
        if (rejected) {
          if (remaining <= 0) throw new IntentRejected('Power owner rejected intent');
          await this.sleep(Math.min(100, remaining));
        } else {
          if (remaining > 0) await this.sleep(remaining);
          return;
        }
      }
    }
  }

  power(id, value, operation, retry) {
    const role = DEVICE_ROLES[String(id).toUpperCase()];
    if (!role) return operation();
    if (typeof value !== 'boolean') return Promise.reject(Error('Expected owner power boolean'));
    if (this.closed || this.closing) return Promise.reject(Error('Power owner closed'));
    const previous = this.queues.get(role) || Promise.resolve();
    const task = previous.catch(() => {}).then(async () => {
      if (this.closed || this.closing) throw Error('Power owner closed');
      if (this.confirmReadback) {
        this.confirmedRoles.set(role, false);
        this.pendingOn.delete(role);
        this.desired.set(role, value);
        this.states.set(role, false);
        this.persist();
        try {
          const before = await this.pauseBeforeOff(role, true);
          const baseline = before ? powerObservation(before, role) : undefined;
          if (this.closed || this.closing) throw Error('Power owner closed');
          const result = await operation();
          const confirmed = await this.verifyPower(role, value, retry, baseline ?? undefined);
          if (this.closed || this.closing) throw Error('Power owner closed');
          this.confirmedRoles.set(role, confirmed);
          if (value) {
            if (confirmed) this.states.set(role, true);
            else this.pendingOn.set(role, { samples: 0 });
            try { await this.heartbeat(); } catch { /* Reconnect needs native On proof. */ }
          }
          return result;
        } catch (error) {
          this.states.set(role, false);
          this.desired.set(role, false);
          this.pendingOn.delete(role);
          try { this.persist(); } catch { /* The writer remains blocked in memory. */ }
          throw error;
        }
      }
      if (!value) {
        this.states.set(role, false);
        this.persist();
        await this.pauseBeforeOff(role);
        if (this.closed || this.closing) throw Error('Power owner closed');
        return operation();
      }
      const result = await operation();
      this.states.set(role, true);
      this.persist();
      try { await this.heartbeat(); } catch { /* Home On can succeed without color. */ }
      return result;
    });
    this.queues.set(role, task);
    return task;
  }

  async close() {
    if (this.closed || this.closing) return;
    this.closing = true;
    this.observers.clear();
    this.pendingOn.clear();
    clearInterval(this.timer);
    try { await this.send(Object.fromEntries([...this.states.keys()].map(role => [role, false])), true); }
    catch { /* An unavailable writer loses its grant without this acknowledgement. */ }
    this.closed = true;
  }
}
