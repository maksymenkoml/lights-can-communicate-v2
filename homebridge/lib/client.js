import { createHash, createHmac, randomBytes } from 'node:crypto';
import { readFileSync, statSync } from 'node:fs';
import { isIPv4 } from 'node:net';

export const CREDENTIAL_FILE = 'lights-can-communicate.json';

/** Read the private core endpoint and owner credential from Homebridge storage. */
export function readCredentials(path) {
  const info = statSync(path);
  if (!info.isFile() || (process.platform !== 'win32' && (info.mode & 0o077) !== 0)) {
    throw new Error('Core credential file must be a private regular file');
  }
  if (info.size > 4096) throw new Error('Core credential file is too large');
  let value;
  try {
    value = JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    throw new Error('Core credential file is not valid JSON');
  }
  if (!value || value.version !== 1 || typeof value.endpoint !== 'string'
    || typeof value.token !== 'string' || !/^[0-9a-f]{32,128}$/.test(value.token)) {
    throw new Error('Core credential file has an invalid structure');
  }
  const url = new URL(value.endpoint);
  if (url.protocol !== 'http:' || url.username || url.password || url.search || url.hash
    || !['', '/'].includes(url.pathname) || !localAddress(url.hostname)) {
    throw new Error('Core endpoint must be a plain HTTP origin on a local address');
  }
  return { endpoint: url.origin, token: value.token };
}

/**
 * The core speaks plain HTTP on the local network. Requests are signed, so
 * the credential itself never travels, and they may only go to loopback or a
 * private LAN address, never to a name that could resolve elsewhere.
 */
export function localAddress(host) {
  if (!isIPv4(host)) return false;
  const [a, b] = host.split('.').map(Number);
  return a === 127 || a === 10 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31);
}

/** HMAC-SHA256 over method, path, time, nonce and body hash (see homekit_power.py). */
export function sign(token, method, path, timestamp, nonce, body) {
  const digest = createHash('sha256').update(body).digest('hex');
  return createHmac('sha256', token)
    .update([method, path, timestamp, nonce, digest].join('\n')).digest('hex');
}

export class CoreClient {
  constructor({ endpoint, token }, {
    fetch = globalThis.fetch, timeout = 8000, now = Date.now,
  } = {}) {
    this.endpoint = endpoint;
    this.token = token;
    this.fetch = fetch;
    this.timeout = timeout;
    this.now = now;
  }

  async request(method, body) {
    const payload = body === undefined ? '' : JSON.stringify(body);
    const timestamp = (this.now() / 1000).toFixed(3);
    const nonce = randomBytes(16).toString('hex');
    const response = await this.fetch(`${this.endpoint}/homekit`, {
      method,
      headers: {
        'Content-Type': 'application/json',
        'X-LCC-Timestamp': timestamp,
        'X-LCC-Nonce': nonce,
        'X-LCC-Signature': sign(this.token, method, '/homekit', timestamp, nonce, payload),
      },
      body: body === undefined ? undefined : payload,
      signal: AbortSignal.timeout(this.timeout),
    });
    const text = await response.text();
    let value;
    try {
      value = JSON.parse(text);
    } catch {
      value = undefined;
    }
    return { status: response.status, value };
  }

  async state() {
    const { status, value } = await this.request('GET');
    if (status !== 200 || !value || value.version !== 1 || typeof value.roles !== 'object') {
      throw new Error(`Core state unavailable (${status})`);
    }
    return value.roles;
  }

  /** Resolve with the new roles state when the core confirmed the intent. */
  async set(intent) {
    const { status, value } = await this.request('POST', intent);
    if (status !== 200 || value?.accepted !== true) {
      throw new Error(`Core did not confirm the HomeKit intent (${status})`);
    }
    return value.roles;
  }
}
