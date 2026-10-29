import test from 'node:test';
import assert from 'node:assert/strict';
import worker, {AuthStore, digest, seal, unseal} from '../src/worker.js';

const origin = 'https://auth.example.test';
const proof = 'a'.repeat(43);
const env = {PUBLIC_ORIGIN: origin, BOX_CLIENT_ID: 'client', BOX_CLIENT_SECRET: 'secret',
  TOKEN_ENCRYPTION_KEY: 'A'.repeat(43), ALLOWED_BOX_USER_IDS: 'owner', DATA_FOLDER_ID: 'data', CONFIG_FOLDER_ID: 'config'};
class Storage {
  constructor() { this.values = new Map(); }
  async get(key) { return structuredClone(this.values.get(key)); }
  async put(key, value) { this.values.set(key, structuredClone(value)); }
  async delete(key) { this.values.delete(key); }
  async list({prefix = '', limit = Infinity} = {}) { return new Map([...this.values].filter(([key]) => key.startsWith(prefix)).slice(0, limit)); }
  async setAlarm(time) { this.alarm = time; }
}
function setup() {
  const storage = new Storage();
  const localEnv = {...env};
  const store = new AuthStore({storage}, localEnv);
  localEnv.AUTH = {idFromName: value => value, get: () => store};
  return {storage, store, env: localEnv};
}
const req = (path, method = 'GET', data, credential) => new Request(origin + path, {method,
  headers: {...(data ? {'Content-Type': 'application/json'} : {}), ...(credential ? {Authorization: 'Bearer ' + credential} : {})},
  ...(data ? {body: JSON.stringify(data)} : {})});
async function start(fixture) {
  const response = await worker.fetch(req('/v1/sessions', 'POST', {code_challenge: await digest(proof)}), fixture.env);
  assert.equal(response.status, 201);
  return response.json();
}
function boxMock(t, {user = 'owner', upload = true, download = true} = {}) {
  let exchanges = 0, refreshes = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === 'https://api.box.com/oauth2/token') {
      assert.equal(options.body.get('client_secret'), 'secret');
      exchanges++;
      if (options.body.get('grant_type') === 'refresh_token') refreshes++;
      return Response.json({access_token: 'access-' + exchanges, refresh_token: 'refresh-' + exchanges, expires_in: 3600});
    }
    if (url.includes('/users/me')) return Response.json({id: user, name: 'Owner'});
    if (url.includes('/folders/data')) return Response.json({permissions: {can_upload: upload}});
    if (url.includes('/folders/config')) return Response.json({permissions: {can_download: download}});
    throw new Error('Unexpected outbound URL');
  });
  return {get exchanges() {return exchanges;}, get refreshes() {return refreshes;}};
}
async function callback(fixture, login) {
  const state = new URL(login.authorization_url).searchParams.get('state');
  return worker.fetch(req('/oauth/callback?' + new URLSearchParams({state, code: 'test-code'})), fixture.env);
}

test('login protects redemption, encrypts persisted tokens, and never returns refresh tokens', async t => {
  const f = setup(); boxMock(t);
  const login = await start(f);
  assert.equal((await worker.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', null, 'b'.repeat(43)), f.env)).status, 401);
  assert.equal((await callback(f, login)).status, 200);
  assert.equal((await callback(f, login)).status, 400);
  const response = await worker.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', null, proof), f.env);
  assert.equal(response.status, 200);
  const value = await response.json();
  assert.equal(value.access_token, 'access-1');
  assert.equal(value.refresh_token, undefined);
  assert.equal(value.project.field_data_folder_id, 'data');
  const persisted = JSON.stringify([...f.storage.values]);
  assert.ok(!persisted.includes('refresh-1'));
  assert.ok(!persisted.includes('access-1'));
  assert.ok(!persisted.includes(proof));
});

test('unapproved Box user is denied and tokens are discarded', async t => {
  const f = setup(); boxMock(t, {user: 'outsider'});
  const login = await start(f);
  assert.equal((await callback(f, login)).status, 403);
  const record = await f.store.read(login.session_id);
  assert.equal(record.status, 'failed');
  assert.equal(record.failure, 'account_not_approved');
  assert.equal(record.token, undefined);
});

test('both data upload and config read permissions are mandatory', async t => {
  const f = setup(); boxMock(t, {download: false});
  assert.equal((await callback(f, await start(f))).status, 403);
});

test('incorrect callback state never exchanges a code', async t => {
  const f = setup(); const calls = boxMock(t);
  const login = await start(f);
  const url = '/oauth/callback?' + new URLSearchParams({state: login.session_id + '.' + 'b'.repeat(43), code: 'test-code'});
  assert.equal((await worker.fetch(req(url), f.env)).status, 400);
  assert.equal(calls.exchanges, 0);
});

test('concurrent renewal consumes rotating refresh token once', async t => {
  const f = setup(); const calls = boxMock(t);
  const login = await start(f); await callback(f, login);
  const session = await f.store.read(login.session_id);
  session.token.expires_at = 0;
  await f.store.save(login.session_id, session);
  const requests = [1, 2].map(() => worker.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', null, proof), f.env));
  const responses = await Promise.all(requests);
  assert.deepEqual(responses.map(r => r.status), [200, 429]);
  assert.equal(calls.refreshes, 1);
  assert.equal((await f.store.read(login.session_id)).token.refresh_token, 'refresh-2');
});

test('restart retains encrypted sessions; removed allow-list entry denies renewal', async t => {
  const f = setup(); boxMock(t);
  const login = await start(f); await callback(f, login);
  const restarted = new AuthStore({storage: f.storage}, {...f.env, ALLOWED_BOX_USER_IDS: 'someone-else'});
  const response = await restarted.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', null, proof));
  assert.equal(response.status, 403);
});

test('disconnect deletes only the authenticated device session', async t => {
  const f = setup(); boxMock(t);
  const login = await start(f); await callback(f, login);
  assert.equal((await worker.fetch(req(`/v1/sessions/${login.session_id}`, 'DELETE', null, proof), f.env)).status, 200);
  assert.equal(await f.store.read(login.session_id), null);
});

test('expired pending sign-in and oversized requests are rejected', async () => {
  const f = setup(); const login = await start(f);
  const session = await f.store.read(login.session_id);
  session.expires_at = 0;
  await f.store.save(login.session_id, session);
  assert.equal((await callback(f, login)).status, 400);
  assert.equal((await worker.fetch(req('/v1/sessions', 'POST', {value: 'x'.repeat(3000)}), f.env)).status, 413);
});

test('API denies browser origins, arbitrary hosts and unknown routes', async () => {
  const f = setup();
  const browser = req('/v1/sessions', 'POST', {code_challenge: proof});
  browser.headers.set('Origin', 'https://evil.example');
  assert.equal((await worker.fetch(browser, f.env)).status, 403);
  assert.equal((await worker.fetch(new Request('https://evil.example/health'), f.env)).status, 400);
  assert.equal((await worker.fetch(req('/v1/admin'), f.env)).status, 404);
});

test('tampering with ciphertext or moving it to a different session fails', async () => {
  const encrypted = await seal(env, 'one', {token: 'private'});
  await assert.rejects(unseal(env, 'two', encrypted));
  encrypted.ciphertext = (encrypted.ciphertext[0] === 'B' ? 'C' : 'B') + encrypted.ciphertext.slice(1);
  await assert.rejects(unseal(env, 'one', encrypted));
});

test('per-IP start rate and expired-record cleanup are enforced', async () => {
  const f = setup();
  for (let i = 0; i < 20; i++) await start(f);
  assert.equal((await worker.fetch(req('/v1/sessions', 'POST', {code_challenge: proof}), f.env)).status, 429);
  const sessions = await f.storage.list({prefix: 'session:'});
  for (const [key] of sessions) {
    const session = await f.store.read(key.slice(8)); session.expires_at = 0; await f.store.save(key.slice(8), session);
  }
  await f.store.alarm();
  assert.equal((await f.storage.list({prefix: 'session:'})).size, 0);
});

test('Box endpoint redirects are not followed and failed exchange discards credentials', async t => {
  const f = setup();
  const login = await start(f);
  let calls = 0;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++;
    assert.equal(url, 'https://api.box.com/oauth2/token');
    assert.equal(options.redirect, 'manual');
    return new Response(null, {status: 302, headers: {Location: 'https://untrusted.example'}});
  });
  assert.equal((await callback(f, login)).status, 401);
  assert.equal(calls, 1);
  const record = await f.store.read(login.session_id);
  assert.equal(record.status, 'failed');
  assert.equal(record.token, undefined);
});

test('authenticated forced refresh rotates once and is limited for one minute', async t => {
  const f = setup(); const calls = boxMock(t);
  const login = await start(f); await callback(f, login);
  const path = `/v1/sessions/${login.session_id}/token`;
  assert.equal((await worker.fetch(req(path, 'POST', {force_refresh: true}, 'b'.repeat(43)), f.env)).status, 401);
  assert.equal(calls.refreshes, 0);
  assert.equal((await worker.fetch(req(path, 'POST', {force_refresh: true}, proof), f.env)).status, 200);
  assert.equal(calls.refreshes, 1);
  const session = await f.store.read(login.session_id);
  session.last_poll = 0;
  await f.store.save(login.session_id, session);
  assert.equal((await worker.fetch(req(path, 'POST', {force_refresh: true}, proof), f.env)).status, 200);
  assert.equal(calls.refreshes, 1);
});

test('forced refresh does not bypass an interrupted rotation', async t => {
  const f = setup(); const calls = boxMock(t);
  const login = await start(f); await callback(f, login);
  const session = await f.store.read(login.session_id);
  session.refreshing = true;
  await f.store.save(login.session_id, session);
  const response = await worker.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', {force_refresh: true}, proof), f.env);
  assert.equal(response.status, 401);
  assert.equal(calls.refreshes, 0);
});

test('token polling accepts an empty POST stream as sent by desktop HTTP clients', async t => {
  const f = setup(); boxMock(t);
  const login = await start(f); await callback(f, login);
  const request = new Request(origin + `/v1/sessions/${login.session_id}/token`, {
    method: 'POST', body: '', headers: {'Content-Type': 'application/json', Authorization: 'Bearer ' + proof},
  });
  assert.equal((await worker.fetch(request, f.env)).status, 200);
});


test('declined consent reports a safe terminal result only to the device proof', async t => {
  const f = setup(); const calls = boxMock(t);
  const login = await start(f);
  const state = new URL(login.authorization_url).searchParams.get('state');
  const response = await worker.fetch(req('/oauth/callback?' + new URLSearchParams({
    state, error: 'access_denied', error_description: '<private-upstream-detail>',
  })), f.env);
  assert.equal(response.status, 400);
  assert.ok(!(await response.text()).includes('private-upstream-detail'));
  assert.equal(calls.exchanges, 0);
  const path = `/v1/sessions/${login.session_id}/token`;
  assert.equal((await worker.fetch(req(path, 'POST', null, 'b'.repeat(43)), f.env)).status, 401);
  const poll = await worker.fetch(req(path, 'POST', null, proof), f.env);
  assert.deepEqual(await poll.json(), {status: 'failed', reason: 'declined'});
  assert.equal((await callback(f, login)).status, 400); // State remains consumed.
  assert.equal(calls.exchanges, 0);
  const record = await f.store.read(login.session_id);
  assert.equal(record.token, undefined);
  assert.equal(record.state_hash, undefined);
  record.expires_at = 0;
  await f.store.save(login.session_id, record);
  await f.store.alarm();
  assert.equal(await f.store.read(login.session_id), null);
});

test('folder denial is distinguishable from account approval and retains no tokens', async t => {
  const f = setup(); boxMock(t, {upload: false});
  const login = await start(f);
  assert.equal((await callback(f, login)).status, 403);
  const response = await worker.fetch(req(`/v1/sessions/${login.session_id}/token`, 'POST', null, proof), f.env);
  assert.deepEqual(await response.json(), {status: 'failed', reason: 'folder_access_required'});
  const record = await f.store.read(login.session_id);
  assert.equal(record.token, undefined);
  assert.equal(record.user, undefined);
  assert.ok(record.expires_at <= Date.now() + 600_000);
  assert.equal((await worker.fetch(req(`/v1/sessions/${login.session_id}`, 'DELETE', null, proof), f.env)).status, 200);
  assert.equal(await f.store.read(login.session_id), null);
});

test('unexpected callback failures retain only a fixed outcome, never upstream details', async t => {
  const f = setup();
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('private-token-and-details'); });
  const login = await start(f);
  assert.equal((await callback(f, login)).status, 503);
  const record = await f.store.read(login.session_id);
  assert.equal(record.failure, 'sign_in_failed');
  assert.ok(!JSON.stringify(record).includes('private-token'));
  assert.equal(record.token, undefined);
});
