import { readFileSync, statSync } from 'node:fs';

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
    || !['', '/'].includes(url.pathname)) {
    throw new Error('Core endpoint must be a plain HTTP origin');
  }
  return { endpoint: url.origin, token: value.token };
}

export class CoreClient {
  constructor({ endpoint, token }, { fetch = globalThis.fetch, timeout = 8000 } = {}) {
    this.endpoint = endpoint;
    this.token = token;
    this.fetch = fetch;
    this.timeout = timeout;
  }

  async request(method, body) {
    const response = await this.fetch(`${this.endpoint}/homekit`, {
      method,
      headers: { 'X-Power-Owner-Token': this.token, 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
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
