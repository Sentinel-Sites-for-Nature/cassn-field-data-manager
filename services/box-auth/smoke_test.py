"""Exercise hosted login without reading or changing FDM's current credentials.

Run with the repository virtualenv. The authorization URL is printed for opening
in a browser. Session proof and Box token stay in memory and are never printed.
"""
import base64
import hashlib
import json
import secrets
import ssl
import time
import urllib.error
import urllib.request
import certifi

ORIGIN = 'https://cassn-csu-box-auth.tiny-band-cf20.workers.dev'

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

opener = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
)

def request(path, method='GET', payload=None, proof=None):
    headers = {'Content-Type': 'application/json', 'User-Agent': 'CASSN-Field-Data-Manager/1.0'}
    if proof:
        headers['Authorization'] = 'Bearer ' + proof
    req = urllib.request.Request(ORIGIN + path, method=method, headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with opener.open(req, timeout=40) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        # Do not print response bodies or sensitive request details.
        return error.code, {}

def main():
    proof = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(proof.encode()).digest()).decode().rstrip('=')
    status, login = request('/v1/sessions', 'POST', {'code_challenge': challenge})
    if status != 201:
        raise SystemExit(f'Could not start hosted sign-in (HTTP {status}).')
    path = '/v1/sessions/' + login['session_id']
    print('Open this Box authorization URL:', flush=True)
    print(login['authorization_url'], flush=True)
    deadline = time.monotonic() + 600
    try:
        while time.monotonic() < deadline:
            time.sleep(3)
            status, result = request(path + '/token', 'POST', proof=proof)
            if status in (202, 429):
                continue
            if status != 200:
                raise SystemExit(f'Hosted sign-in failed (HTTP {status}).')
            print('Hosted sign-in verified for:', result['user']['name'], flush=True)
            print('Project:', result['project']['name'], flush=True)
            print('Data folder:', result['project']['field_data_folder_id'], flush=True)
            print('Config folder:', result['project']['app_config_folder_id'], flush=True)
            print('Access-token lifetime:', result['expires_in'], 'seconds', flush=True)
            status, _ = request(path + '/token', 'POST', proof=secrets.token_urlsafe(32))
            assert status == 401, 'Wrong proof was not rejected'
            print('Unauthorized device proof rejected.', flush=True)
            return
        raise SystemExit('Sign-in timed out.')
    finally:
        status, _ = request(path, 'DELETE', proof=proof)
        print('Test session removed.' if status == 200 else 'Test session cleanup not confirmed.', flush=True)

if __name__ == '__main__':
    main()
