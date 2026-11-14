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
      this.states.set(role, this.persisted[role] ?? currentOn === true);
    }
    return true;
  }

  persist() {
    if (!this.statePath) return;
    Object.assign(this.persisted, Object.fromEntries(this.states));
    const temporary = `${this.statePath}.tmp-${randomUUID()}`;
    try {
      writeFileSync(temporary, JSON.stringify(this.persisted), { mode: 0o600, flag: 'wx' });
      renameSync(temporary, this.statePath);
    } finally {
      rmSync(temporary, { force: true });
    }
  }

  async send(states, closing = false) {
    if (this.closed || (this.closing && !closing) || !Object.keys(states).length) return false;
    const message = { session: this.session, sequence: ++this.sequence,
      issued_at: this.wallClock() / 1000, states };
    for (const [role, on] of Object.entries(states)) {
      if (on) this.lastGrant.set(role, this.clock());
    }
    try {
      const result = await this.request(message);
      if (!result || result.accepted !== true) throw new IntentRejected('Power owner rejected intent');
      this.lastAcknowledgement = Math.max(this.lastAcknowledgement, message.sequence);
      return true;
    } catch (error) {
      if (error instanceof IntentRejected) {
        this.lastRejection = Math.max(this.lastRejection, message.sequence);
      }
      throw error;
    }
  }

  heartbeat() { return this.send(Object.fromEntries(this.states)); }

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

  async pauseBeforeOff(role) {
    const deadline = Math.max(this.startedAt, this.lastGrant.get(role) ?? -Infinity)
      + SAFE_GRANT_EXPIRY_MS;
    let rejected = this.lastRejection > this.lastAcknowledgement;
    while (true) {
      if (this.closed || this.closing) throw Error('Power owner closed');
      try { await this.heartbeat(); return; }
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

  power(id, value, operation) {
    const role = DEVICE_ROLES[String(id).toUpperCase()];
    if (!role) return operation();
    if (typeof value !== 'boolean') return Promise.reject(Error('Expected owner power boolean'));
    if (this.closed || this.closing) return Promise.reject(Error('Power owner closed'));
    const previous = this.queues.get(role) || Promise.resolve();
    const task = previous.catch(() => {}).then(async () => {
      if (this.closed || this.closing) throw Error('Power owner closed');
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
    clearInterval(this.timer);
    try { await this.send(Object.fromEntries([...this.states.keys()].map(role => [role, false])), true); }
    catch { /* An unavailable writer loses its grant without this acknowledgement. */ }
    this.closed = true;
  }
}
