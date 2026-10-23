# Box authentication services

A Cloudflare Worker and one SQLite-backed Durable Object handle user OAuth for
UC and CSU Field Data Manager. The desktop never receives the Box client secret or
refresh token. Media uploads go directly from the desktop to Box.

## Prototype configuration

- Worker: `cassn-csu-box-auth`
- Origin: `https://cassn-csu-box-auth.tiny-band-cf20.workers.dev`
- Box callback: `https://cassn-csu-box-auth.tiny-band-cf20.workers.dev/oauth/callback`
- Approved Box user: John Imperato (`45181112727`)
- CSU data: `430058250915`
- CSU app configuration: `430057609739`
- The folders remain owned by John's UC Davis account. They are currently empty.

The allow-list and actual Box folder permissions are both required. Adding a
user to the allow-list never grants Box permissions. No service account is used.
The current Box OAuth consent grants read/write access across the signed-in
user's accessible Box content. Checking CSU folder permissions does not restrict
the returned access token to those folders. Folder-restricted token issuance is
not implemented in this prototype.
The standalone desktop prototypes use this protocol through `cassn/box/hosted.py`.
Existing local-secret installations remain available. The unified desktop selects
UC or CSU before loading project settings. Run `python -m cassn.launcher` from
the repository root, or build with `.venv/bin/python utils/build_macos.py`.
The packaged app and ZIP are written to `dist/unified/`.

UC uses `wrangler.uc.json` and `https://cassn-uc-box-auth.tiny-band-cf20.workers.dev`,
with data `377166707326` and app_config `377164694031`. Its sessions and encryption
key are separate from CSU. Both profiles currently allow only John.

## Deploy

Use Node 22 or newer. Run `npm install`, `npm test`, and `npm run check` here.
Authenticate Wrangler to the existing account. `npm run deploy` creates the
Worker and its SQLite Durable Object namespace; no paid plan is needed.

Configure these encrypted Worker secrets (never in source, command arguments,
plaintext variables, or log output):

- `BOX_CLIENT_SECRET`: secret for the configured Box OAuth client.
- `TOKEN_ENCRYPTION_KEY`: 32 random bytes encoded as unpadded base64url.

Register the exact hosted callback above in the Box app's redirect URI list,
retaining the existing local callback. Verify `/health` returns `ready: true`,
then test a fresh browser login from the desktop client.

## Desktop protocol

1. Generate a random 32-byte device proof and encode as unpadded base64url.
2. POST `/v1/sessions` with JSON `{ "code_challenge": "<base64url SHA256(proof)>" }`.
3. Open the returned `authorization_url` in the system browser.
4. Poll POST `/v1/sessions/<session_id>/token` every 3 seconds, with
   `Authorization: Bearer <proof>`. HTTP 202 means sign-in is pending.
   HTTP 200 with `status: "failed"` ends the attempt. `reason` is one of
   `declined`, `account_not_approved`, `folder_access_required`, or
   `sign_in_failed`. Delete that session and show the corresponding recovery
   instructions. Failed callback records contain no Box tokens or user details,
   expire within ten minutes, and require the same device proof to read/delete.
5. A successful response contains the access token, expiry, user name, and
   project folder IDs. Store the session ID/proof in the OS credential store.
6. Request another token when the previous one nears expiry. The service
   renews it, validates Box access, and returns the new access token. For an SDK
   retry after an early 401, send JSON `{ "force_refresh": true }`; authenticated
   forced rotation is limited to once per minute. Normal polling sends `{}`.
   An empty POST stream is also accepted.
7. DELETE `/v1/sessions/<session_id>` with the same proof disconnects that device.
   It deletes hosted credentials; it does not revoke other Box sessions. An
   already issued Box access token can remain valid until Box expires/revokes it.

This proof protects desktop redemption of a session. It is not Box PKCE:
Box's authorization-code exchange still uses the server-held client secret.

Pending sign-ins expire after 10 minutes. Connected sessions expire after seven
days and require another browser sign-in. Expired records are cleaned up by an
hourly alarm. Token records are encrypted with AES-256-GCM and bound to their
session IDs. The service serializes token rotation and stores rotation intent
before contacting Box; an interrupted rotation requires sign-in again.

## Limits and operations

Pilot caps: 100 stored sessions, 200 sign-in starts per 24-hour window, 20 starts
per hashed IP per hour, and at least two seconds between polls. These are app
limits, deliberately lower than Cloudflare quotas. No raw IPs are retained.
A single Durable Object serializes calls; this is suitable for a small pilot,
not a high-throughput authentication platform.

Cloudflare Free: 100,000 Worker requests/day, 10 ms CPU/request; SQLite Durable
Objects: 100,000 requests/day, 13,000 GB-seconds/day, 5 million rows read/day,
100,000 rows written/day, 5 GB storage. Account-wide use counts toward quotas.
Exhausted free quotas cause errors rather than automatic paid upgrades.

Request logging/observability is disabled because OAuth callback URLs contain
codes. Never enable request/body logging or live tail during real sign-in.
Errors are sanitized. The service only calls fixed Box endpoints, rejects
untrusted origins and browser API requests, and uses no arbitrary redirects.

To stop issuing tokens, empty `ALLOWED_BOX_USER_IDS` and redeploy. Existing Box
access tokens remain subject to Box permissions and expiry. Keep the encryption
key stable during updates; replacing it makes existing hosted sessions unreadable.
No production rollout to additional users should occur before end-to-end testing
of the desktop integration, renewal, expiry, disconnect, and permission removal.
See [CSU onboarding](../../docs/csu_box_onboarding.md) for the per-user checklist
and the service-before-desktop update order for terminal failure responses.

## Validation

`npm test` covers permission denial, callback state/replay, device-proof theft
resistance, encrypted storage, concurrent rotation, process restart, expiration,
disconnect, origin rejection, size limits, rate limits, and cleanup. These tests
mock Box. The live browser test below additionally validates Box app setup.

Deployment status (2026-10-22): Worker and encrypted secrets deployed; live health,
session creation, pending polling, and deletion verified. Hosted callback saved
in Box. End-to-end browser sign-in succeeded as John Imperato after explicit
approval of the account-wide Box scope. Both CSU folder permissions were checked,
a wrong device proof was rejected, and the hosted test session was deleted.
Fifteen service tests now pass, including forced rotation and empty desktop POST
streams. See the desktop validation record for current live test results.
Cloudflare outbound requests use manual redirect handling and reject non-success
responses; the Fetch error redirect mode caused the initial runtime failure.
