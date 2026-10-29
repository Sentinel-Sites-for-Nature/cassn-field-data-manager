// Project pilot: user OAuth, no service-account impersonation or media proxying.
const LOGIN_TTL = 10 * 60_000;
const SESSION_TTL = 7 * 24 * 60 * 60_000;
const MAX_SESSIONS = 100;
const MAX_BODY = 2048;
const ID = /^[a-f0-9]{32}$/;
const PROOF = /^[A-Za-z0-9_-]{43}$/;
// Fixed outcomes only: never persist an upstream exception or failed OAuth tokens.
const loginFailures = {
  sign_in_declined: 'declined',
  user_not_permitted: 'account_not_approved',
  box_access_required: 'folder_access_required',
};
const headers = {
  'Cache-Control': 'no-store',
  'Referrer-Policy': 'no-referrer',
  'X-Content-Type-Options': 'nosniff',
  'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
  'Strict-Transport-Security': 'max-age=31536000',
};
const encoder = new TextEncoder();
const b64 = bytes => btoa(String.fromCharCode(...new Uint8Array(bytes))).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
const unb64 = str => Uint8Array.from(atob(str.replaceAll('-', '+').replaceAll('_', '/')), c => c.charCodeAt(0));
const random = () => b64(crypto.getRandomValues(new Uint8Array(32)));
export const digest = async value => b64(await crypto.subtle.digest('SHA-256', encoder.encode(value)));
const json = (body, status = 200) => new Response(JSON.stringify(body), {status, headers: {...headers, 'Content-Type': 'application/json'}});
const page = (title, message, status = 200) => new Response(`<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>${title}</title><style>body{font:18px system-ui;max-width:650px;margin:12vh auto;padding:24px;color:#20352d;background:#f5f8f6}h1{font-size:30px}p{line-height:1.6}</style><h1>${title}</h1><p>${message}</p></html>`, {status, headers: {...headers, 'Content-Type': 'text/html; charset=utf-8'}});
class AuthError extends Error {
  constructor(status, code) { super(code); this.status = status; }
}
const fail = (status, code) => { throw new AuthError(status, code); };
const allowed = (env, id) => (env.ALLOWED_BOX_USER_IDS || '').split(',').map(s => s.trim()).includes(id);
const configured = env => env.BOX_CLIENT_SECRET && PROOF.test(env.TOKEN_ENCRYPTION_KEY || '') && env.BOX_CLIENT_ID && env.DATA_FOLDER_ID && env.CONFIG_FOLDER_ID && env.ALLOWED_BOX_USER_IDS;
async function body(request, allowEmpty = false) {
  if (!request.headers.get('content-type')?.startsWith('application/json')) fail(415, 'json_required');
  const reader = request.body?.getReader();
  if (!reader) fail(400, 'invalid_request');
  let size = 0, chunks = [];
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_BODY) { await reader.cancel(); fail(413, 'request_too_large'); }
    chunks.push(value);
  }
  if (allowEmpty && size === 0) return {};
  const bytes = new Uint8Array(size); let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  try { return JSON.parse(new TextDecoder().decode(bytes)); } catch { fail(400, 'invalid_json'); }
}
async function encryptionKey(env) {
  return crypto.subtle.importKey('raw', unb64(env.TOKEN_ENCRYPTION_KEY), 'AES-GCM', false, ['encrypt', 'decrypt']);
}
export async function seal(env, id, value) {
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = await crypto.subtle.encrypt({name: 'AES-GCM', iv, additionalData: encoder.encode(id)}, await encryptionKey(env), encoder.encode(JSON.stringify(value)));
  return {iv: b64(iv), ciphertext: b64(ciphertext)};
}
export async function unseal(env, id, envelope) {
  const plain = await crypto.subtle.decrypt({name: 'AES-GCM', iv: unb64(envelope.iv), additionalData: encoder.encode(id)}, await encryptionKey(env), unb64(envelope.ciphertext));
  return JSON.parse(new TextDecoder().decode(plain));
}
async function boxFetch(path, token) {
  const response = await fetch('https://api.box.com/2.0/' + path, {
    headers: {Authorization: `Bearer ${token}`}, redirect: 'manual', signal: AbortSignal.timeout(10_000),
  });
  if ([401, 403, 404].includes(response.status)) fail(403, 'box_access_required');
  if (!response.ok) fail(503, 'box_unavailable');
  return response.json();
}
async function checkAccess(env, accessToken, expectedId) {
  const user = await boxFetch('users/me?fields=id,name,login', accessToken);
  if (!allowed(env, user.id) || (expectedId && user.id !== expectedId)) fail(403, 'user_not_permitted');
  const data = await boxFetch(`folders/${env.DATA_FOLDER_ID}?fields=id,permissions`, accessToken);
  const config = await boxFetch(`folders/${env.CONFIG_FOLDER_ID}?fields=id,permissions`, accessToken);
  if (!data.permissions?.can_upload || !config.permissions?.can_download) fail(403, 'box_access_required');
  return {id: user.id, name: user.name};
}
async function exchange(env, values) {
  const response = await fetch('https://api.box.com/oauth2/token', {
    method: 'POST', redirect: 'manual', signal: AbortSignal.timeout(10_000),
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({client_id: env.BOX_CLIENT_ID, client_secret: env.BOX_CLIENT_SECRET, ...values}),
  });
  if (!response.ok) fail(response.status >= 500 || response.status === 429 ? 503 : 401, 'box_sign_in_required');
  const token = await response.json();
  if (!token.access_token || !token.refresh_token || !(token.expires_in > 0)) fail(503, 'invalid_box_response');
  return {...token, expires_at: Date.now() + token.expires_in * 1000};
}
// Never log Box errors, codes, requests, credentials, or token responses.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.origin !== env.PUBLIC_ORIGIN) return json({error: 'invalid_origin'}, 400);
    if (request.method === 'GET' && url.pathname === '/') return page('Field Data Manager', 'Open Field Data Manager and click Connect to Box to sign in with your approved project account. Contact John if you need project access.');
    if (request.method === 'GET' && url.pathname === '/health') return json({service: new URL(env.PUBLIC_ORIGIN).hostname.split('.')[0], ready: !!configured(env)}, configured(env) ? 200 : 503);
    if (!configured(env)) return json({error: 'service_not_configured'}, 503);
    // Only the fixed callback is browser-accessible. Desktop API has no CORS support.
    if (request.headers.has('origin') && url.pathname !== '/oauth/callback') return json({error: 'desktop_client_required'}, 403);
    const validRoute = (url.pathname === '/v1/sessions' && request.method === 'POST') ||
      (/^\/v1\/sessions\/[a-f0-9]{32}\/token$/.test(url.pathname) && request.method === 'POST') ||
      (/^\/v1\/sessions\/[a-f0-9]{32}$/.test(url.pathname) && request.method === 'DELETE') ||
      (url.pathname === '/oauth/callback' && request.method === 'GET');
    if (!validRoute) return json({error: 'not_found'}, 404);
    try {
      return await env.AUTH.get(env.AUTH.idFromName('csu-pilot')).fetch(request);
    } catch { return json({error: 'service_unavailable'}, 503); }
  },
};
export class AuthStore {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; this.queue = Promise.resolve(); }
  // OAuth refresh tokens rotate. Serialize across all awaits, including network calls.
  fetch(request) {
    const run = this.queue.then(() => this.handle(request));
    this.queue = run.catch(() => {});
    return run;
  }
  async read(id) {
    const envelope = await this.ctx.storage.get('session:' + id);
    return envelope ? unseal(this.env, id, envelope) : null;
  }
  async save(id, session) {
    await this.ctx.storage.put('session:' + id, await seal(this.env, id, session));
  }
  async cleanup() {
    for (const [key, envelope] of await this.ctx.storage.list({prefix: 'session:'})) {
      const session = await unseal(this.env, key.slice(8), envelope);
      if (session.expires_at <= Date.now()) await this.ctx.storage.delete(key);
    }
    for (const [key, value] of await this.ctx.storage.list({prefix: 'rate:'})) {
      if (value.reset <= Date.now()) await this.ctx.storage.delete(key);
    }
  }
  async alarm() {
    const run = this.queue.then(async () => {
      await this.cleanup();
      if ((await this.ctx.storage.list({limit: 1})).size) await this.ctx.storage.setAlarm(Date.now() + 60 * 60_000);
    });
    this.queue = run.catch(() => {});
    return run;
  }
  async rate(key, limit, period) {
    const now = Date.now();
    let value = await this.ctx.storage.get(key);
    if (!value || value.reset <= now) value = {count: 0, reset: now + period};
    if (value.count >= limit) fail(429, 'try_again_later');
    value.count++;
    await this.ctx.storage.put(key, value);
  }
  async handle(request) {
    const url = new URL(request.url);
    try {
      if (url.pathname === '/v1/sessions' && request.method === 'POST') return await this.start(request);
      if (url.pathname === '/oauth/callback' && request.method === 'GET') return await this.callback(url);
      const parts = url.pathname.split('/');
      const id = parts[3];
      if (!ID.test(id || '')) fail(404, 'not_found');
      const proof = request.headers.get('authorization')?.replace(/^Bearer /, '') || '';
      if (!PROOF.test(proof)) fail(401, 'invalid_session');
      const session = await this.read(id);
      if (!session || session.challenge !== await digest(proof)) fail(401, 'invalid_session');
      if (session.expires_at <= Date.now()) { await this.ctx.storage.delete('session:' + id); fail(401, 'session_expired'); }
      if (request.method === 'DELETE') {
        // Disconnect this device only; never revoke other sessions for the same Box app.
        await this.ctx.storage.delete('session:' + id);
        return json({disconnected: true});
      }
      if (Date.now() - (session.last_poll || 0) < 2000) fail(429, 'poll_too_fast');
      session.last_poll = Date.now();
      if (session.status === 'pending') { await this.save(id, session); return json({status: 'pending'}, 202); }
      if (session.status === 'failed') {
        await this.save(id, session);
        return json({status: 'failed', reason: session.failure});
      }
      if (session.status !== 'connected' || !allowed(this.env, session.user.id)) fail(403, 'user_not_permitted');
      let forceRefresh = false;
      if (request.body) {
        const input = await body(request, true);
        if (!input || typeof input !== 'object' || Array.isArray(input) ||
            Object.keys(input).some(key => key !== 'force_refresh') ||
            (input.force_refresh !== undefined && typeof input.force_refresh !== 'boolean')) fail(400, 'invalid_request');
        forceRefresh = input.force_refresh === true && Date.now() - (session.last_refresh || 0) >= 60_000;
      }
      if (session.token.expires_at <= Date.now() + 90_000 || forceRefresh) {
        // Persist rotation intent first. If the process dies after exchange, require login
        // instead of reusing a refresh token that Box may already have consumed.
        if (session.refreshing) fail(401, 'box_sign_in_required');
        session.refreshing = true;
        await this.save(id, session);
        session.token = await exchange(this.env, {grant_type: 'refresh_token', refresh_token: session.token.refresh_token});
        session.refreshing = false;
        session.last_refresh = Date.now();
        await this.save(id, session);
      }
      await checkAccess(this.env, session.token.access_token, session.user.id);
      await this.save(id, session);
      return json({status: 'connected', access_token: session.token.access_token,
        expires_in: Math.max(0, Math.floor((session.token.expires_at - Date.now()) / 1000)),
        session_expires_at: session.expires_at, user: session.user,
        project: {name: 'CASSN', field_data_folder_id: this.env.DATA_FOLDER_ID, app_config_folder_id: this.env.CONFIG_FOLDER_ID}});
    } catch (error) {
      const status = error instanceof AuthError ? error.status : 503;
      const code = error instanceof AuthError ? error.message : 'service_unavailable';
      if (url.pathname === '/oauth/callback') return page('Connection not completed', 'Return to Field Data Manager for instructions. Check that you used the Box account approved for your project and accepted both folder invitations. If this continues, contact John.', status);
      return json({error: code}, status);
    }
  }
  async start(request) {
    const input = await body(request, true);
    if (!input || !PROOF.test(input.code_challenge || '')) fail(400, 'invalid_challenge');
    await this.cleanup();
    await this.rate('rate:global', 200, 24 * 60 * 60_000);
    // Hash IP with a server secret; no raw IP is persisted.
    const ipKey = await digest(this.env.TOKEN_ENCRYPTION_KEY + (request.headers.get('CF-Connecting-IP') || 'unknown'));
    await this.rate('rate:ip:' + ipKey, 20, 60 * 60_000);
    if ((await this.ctx.storage.list({prefix: 'session:', limit: MAX_SESSIONS})).size >= MAX_SESSIONS) fail(429, 'session_capacity');
    const id = crypto.randomUUID().replaceAll('-', '');
    const stateProof = random();
    await this.save(id, {status: 'pending', challenge: input.code_challenge, state_hash: await digest(stateProof), expires_at: Date.now() + LOGIN_TTL});
    await this.ctx.storage.setAlarm(Date.now() + 60 * 60_000);
    const authorize = new URL('https://account.box.com/api/oauth2/authorize');
    authorize.search = new URLSearchParams({response_type: 'code', client_id: this.env.BOX_CLIENT_ID,
      redirect_uri: this.env.PUBLIC_ORIGIN + '/oauth/callback', state: id + '.' + stateProof}).toString();
    return json({session_id: id, authorization_url: authorize.href, expires_in: LOGIN_TTL / 1000, poll_interval: 3}, 201);
  }
  async callback(url) {
    if (url.searchParams.getAll('state').length !== 1) fail(400, 'invalid_state');
    const [id, proof, extra] = url.searchParams.get('state').split('.');
    if (!ID.test(id || '') || !PROOF.test(proof || '') || extra !== undefined) fail(400, 'invalid_state');
    const session = await this.read(id);
    if (!session || session.status !== 'pending' || session.expires_at <= Date.now() || session.state_hash !== await digest(proof)) fail(400, 'invalid_state');
    session.status = 'exchanging';
    delete session.state_hash;
    await this.save(id, session); // consume state once, even if exchange fails
    let stage = 'token_exchange';
    try {
      if (url.searchParams.has('error')) fail(400, 'sign_in_declined');
      if (url.searchParams.getAll('code').length !== 1 || !url.searchParams.get('code') || url.searchParams.get('code').length > 1024) fail(400, 'invalid_code');
      session.token = await exchange(this.env, {grant_type: 'authorization_code', code: url.searchParams.get('code'), redirect_uri: this.env.PUBLIC_ORIGIN + '/oauth/callback'});
      stage = 'access_check';
      session.user = await checkAccess(this.env, session.token.access_token);
      session.status = 'connected';
      session.expires_at = Date.now() + SESSION_TTL;
      stage = 'session_save';
      await this.save(id, session);
    } catch (error) {
      // Keep a short-lived, proof-protected result so the desktop can explain
      // failure. Replace the entire record to discard any acquired Box tokens.
      await this.save(id, {status: 'failed', challenge: session.challenge,
        expires_at: Math.min(session.expires_at, Date.now() + LOGIN_TTL),
        failure: loginFailures[error instanceof AuthError ? error.message : ''] || 'sign_in_failed'});
      if (!(error instanceof AuthError)) fail(503, stage + '_unavailable');
      throw error;
    }
    return page('Connected to Box', 'You can return to Field Data Manager. This browser tab can be closed.');
  }
}
