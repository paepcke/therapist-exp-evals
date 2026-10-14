/**
 * **********************************************************
 * @Author: Andreas Paepcke
 * @Date:   2026-10-11 16:46:16
 * @File:   /Users/paepcke/VSCodeWorkspaces/therapist-exp-evals/src/evals_service/static/api.mjs
 * @Last Modified by:   Andreas Paepcke
 * @Last Modified time: 2026-10-11 16:46:16
 * **********************************************************
 */

'use strict';

/*
 * Talks to the server. Holds the device token in localStorage, so that a
 * person who logged in once on this browser comes back without a password.
 * Every storage access is guarded: it can throw in private windows.
 */

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export class Api {
  static TOKEN_KEY = 'evalsDeviceToken';

  constructor() {
    this.onLoggedOut = () => {};
  }

  get token() {
    try { return localStorage.getItem(Api.TOKEN_KEY); } catch (e) { return this._memoryToken || null; }
  }

  set token(value) {
    this._memoryToken = value;
    try {
      if (value) localStorage.setItem(Api.TOKEN_KEY, value);
      else localStorage.removeItem(Api.TOKEN_KEY);
    } catch (e) { /* the token then lives only as long as this page */ }
  }

  /**
   * @param {string} method
   * @param {string} path
   * @param {Object|null} body
   * @param {Object} options  keepalive: let the request finish while the page closes
   * @returns {Promise<Object>} the parsed JSON reply
   */
  async request(method, path, body = null, options = {}) {
    const headers = {};
    if (this.token) headers['Authorization'] = `Bearer ${this.token}`;
    if (body !== null) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(path, {
        method, headers, body: body === null ? undefined : JSON.stringify(body),
        keepalive: !!options.keepalive, cache: 'no-store',
      });
    } catch (e) {
      throw new ApiError(0, 'The server cannot be reached. Please check your connection.');
    }
    let doc = {};
    try { doc = await response.json(); } catch (e) { /* not JSON */ }
    if (response.status === 401 && this.token) {
      this.token = null;
      this.onLoggedOut();
    }
    if (!response.ok) throw new ApiError(response.status, doc.error || `The server answered ${response.status}.`);
    return doc;
  }

  async login(email, password) {
    const doc = await this.request('POST', '/api/login', { email, password });
    this.token = doc.token;
    return doc.me;
  }

  async register(email, code, password) {
    const doc = await this.request('POST', '/api/register', { email, code, password });
    this.token = doc.token;
    return doc.me;
  }

  async logout() {
    try { await this.request('POST', '/api/logout', {}); } catch (e) { /* the token goes either way */ }
    this.token = null;
  }

  /** @returns {Promise<Blob>} the administrator's CSV of all submitted ratings */
  async exportCsv() {
    const response = await fetch('/api/admin/export.csv', {
      headers: { Authorization: `Bearer ${this.token}` }, cache: 'no-store',
    });
    if (!response.ok) throw new ApiError(response.status, 'The export failed.');
    return response.blob();
  }
}
