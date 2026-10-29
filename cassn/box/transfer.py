"""Upload-only SDK transport: one retry owner, cancellable waits and measurements.

No credentials or network access occur at import time. Ordinary Box utilities keep
using the SDK's default transport; the deployment uploader opts into this one.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import copy
from email.utils import parsedate_to_datetime
import math
import random
import threading
import time
from urllib.parse import urlsplit

from box_sdk_gen.networking.box_network_client import BoxNetworkClient
from box_sdk_gen.networking.network_client import NetworkClient
from box_sdk_gen.networking.retries import RetryStrategy
from box_sdk_gen.box.errors import BoxSDKError
from requests import RequestException


class UploadCancelled(RuntimeError):
    pass


class UploadPaused(RuntimeError):
    """Keep staging/recovery records; resume rather than restarting blindly."""


def retry_after(headers, *, now=None):
    """A valid server minimum in seconds, or None. Header names ignore case."""
    value = next((v for k, v in (headers or {}).items()
                  if str(k).lower() == 'retry-after'), None)
    if value is None:
        return None
    try:
        delay = float(value)
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(str(value))
            if date.tzinfo is None:
                return None
            delay = max(0.0, date.timestamp() - (time.time() if now is None else now))
        except (TypeError, ValueError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def error_response(exc):
    info = getattr(exc, 'response_info', None) or getattr(exc, 'response', None)
    status = (getattr(info, 'status_code', None) or getattr(exc, 'status_code', None)
              or getattr(exc, 'status', None) or 0)
    return status, getattr(info, 'headers', None) or getattr(exc, 'headers', None) or {}


def uncertain_request(exc):
    """Only transport/server outcomes qualify, not arbitrary programming errors."""
    status, _ = error_response(exc)
    return status in (408, 409, 500, 502, 503, 504) or (
        status == 0 and isinstance(exc, (BoxSDKError, RequestException, ConnectionError, TimeoutError)))


class Cooldown:
    def __init__(self):
        self.lock = threading.Lock()
        self.deadline = 0.0
        self.refresh_lock = threading.Lock()
        self.auth_generation = 0

    def defer(self, seconds, clock):
        with self.lock:
            self.deadline = max(self.deadline, clock() + seconds)

    def remaining(self, clock):
        with self.lock:
            return max(0.0, self.deadline - clock())


_ACTORS = {}
_ACTORS_LOCK = threading.Lock()


class UploadMetrics:
    def __init__(self, *, clock=time.monotonic, callback=None):
        self.clock, self.callback = clock, callback
        self.lock = threading.Lock()
        self.started = self.stage_started = clock()
        self.stage = 'preparing'
        self.durations = Counter()
        self.counts = Counter()
        self.last_emit = -math.inf
        self.output_path = None
        self.last_write = -math.inf
        self.write_lock = threading.Lock()

    def add(self, **values):
        with self.lock:
            self.counts.update(values)
            for name in ('pending_jobs', 'active_requests'):
                if name in values:
                    peak = 'peak_' + name
                    self.counts[peak] = max(self.counts[peak], self.counts[name])
        self.emit()

    def set_stage(self, stage):
        with self.lock:
            now = self.clock()
            self.durations[self.stage] += now - self.stage_started
            self.stage_started, self.stage = now, stage
        self.emit(force=True)

    def snapshot(self):
        with self.lock:
            now = self.clock()
            elapsed = max(now - self.started, 0.001)
            durations = dict(self.durations)
            durations[self.stage] = durations.get(self.stage, 0) + now - self.stage_started
            result = dict(self.counts)
            result.update(stage=self.stage, elapsed_seconds=elapsed, stage_seconds=durations,
                          acknowledged_MB_per_second=self.counts['acknowledged_bytes'] / elapsed / 1e6,
                          files_per_second=self.counts['uploaded_files'] / elapsed)
            return result

    def emit(self, *, force=False):
        if self.callback is None and self.output_path is None:
            return
        with self.lock:
            now = self.clock()
            if not force and now - self.last_emit < 0.5:
                return
            self.last_emit = now
        snapshot = self.snapshot()
        if self.output_path is not None:
            with self.write_lock:
                if force or now - self.last_write >= 30:
                    try:
                        from cassn.box.upload_state import atomic_json
                        atomic_json(self.output_path, snapshot)
                        self.last_write = now
                    except OSError:
                        # Diagnostics must never turn an acknowledged write into
                        # a failed request. Surface the count in the next snapshot.
                        with self.lock:
                            self.counts['diagnostic_write_errors'] += 1
        if self.callback is not None:
            self.callback(snapshot)


class UploadRuntime:
    """One operation's bounded resources; cooldown/auth generation shared per actor."""
    def __init__(self, *, cancel=None, transfers=8, part_workers=2,
                 buffer_bytes=128 * 1024 * 1024, clock=time.monotonic,
                 wait=None, metrics=None, max_wait=3600):
        if transfers < 1 or part_workers < 1 or part_workers > 4 or buffer_bytes < 1:
            raise ValueError('Invalid Box upload concurrency/buffer settings')
        self.cancel = cancel if cancel is not None else threading.Event()
        self.transfers, self.part_workers = transfers, part_workers
        self.clock = clock
        self.wait = wait if wait is not None else self.cancel.wait
        self.max_wait = max_wait
        self.metrics = metrics or UploadMetrics(clock=clock)
        self.cooldown = Cooldown()
        self.actor = None
        self.slots = threading.BoundedSemaphore(transfers)
        self.local = threading.local()
        self.buffer_limit = buffer_bytes
        self.buffer_used = 0
        self.buffer_condition = threading.Condition()

    def bind_actor(self, actor):
        if not actor:
            raise UploadPaused('Box did not identify the authenticated upload account')
        self.actor = str(actor)
        with _ACTORS_LOCK:
            self.cooldown = _ACTORS.setdefault(self.actor, Cooldown())

    def check_cancelled(self):
        if self.cancel.is_set():
            raise UploadCancelled('Upload cancelled; local files and recovery records retained')

    def sleep(self, seconds):
        self.check_cancelled()
        if seconds > self.max_wait:
            raise UploadPaused('Box requested a long wait; upload paused with recovery records retained')
        self.metrics.add(waiting=1)
        started = self.clock()
        try:
            if self.wait(seconds):
                raise UploadCancelled('Upload cancelled while waiting for Box')
            self.check_cancelled()
        finally:
            self.metrics.add(waiting=-1, retry_wait_seconds=max(0, self.clock() - started))

    def wait_cooldown(self):
        deadline = self.clock() + self.max_wait
        while (delay := self.cooldown.remaining(self.clock)) > 0:
            if self.clock() + delay > deadline:
                raise UploadPaused('Box cooldown exceeded the upload wait budget; resume later')
            self.sleep(delay)

    @contextmanager
    def connection(self):
        # Token refresh can use this adapter recursively on the same thread.
        depth = getattr(self.local, 'depth', 0)
        if depth:
            self.local.depth += 1
            try:
                yield
            finally:
                self.local.depth -= 1
            return
        while not self.slots.acquire(timeout=0.1):
            self.check_cancelled()
        self.local.depth = 1
        try:
            self.check_cancelled()
            # Recheck after acquiring: another request may just have received 429.
            self.wait_cooldown()
            self.metrics.add(active_requests=1)
            try:
                yield
            finally:
                self.metrics.add(active_requests=-1)
        finally:
            self.local.depth = 0
            self.slots.release()

    def reserve_buffer(self, size):
        if size > self.buffer_limit:
            raise UploadPaused('Box part size exceeds the configured upload buffer budget')
        with self.buffer_condition:
            while self.buffer_used + size > self.buffer_limit:
                self.check_cancelled()
                self.buffer_condition.wait(timeout=0.1)
            self.check_cancelled()
            self.buffer_used += size

    def release_buffer(self, size):
        with self.buffer_condition:
            self.buffer_used -= size
            self.buffer_condition.notify_all()


class _NoRetry(RetryStrategy):
    def should_retry(self, **kwargs):
        return False

    def retry_after(self, **kwargs):
        return 0


def _streams(options):
    result = [options.file_stream] if options.file_stream is not None else []
    result += [p.file_stream for p in options.multipart_data or [] if p.file_stream is not None]
    return result


def _positions(options):
    positions, size = [], 0
    for stream in _streams(options):
        # Non-replayable bodies may be sent once, never retried automatically.
        if not stream.seekable():
            return None, 0
        position = stream.tell()
        stream.seek(0, 2)
        size += stream.tell() - position
        stream.seek(position)
        positions.append((stream, position))
    return positions, size


class UploadNetworkClient(NetworkClient):
    def __init__(self, runtime, *, client_factory=None, attempts=5):
        super().__init__()
        self.runtime, self.attempts = runtime, attempts
        self.client_factory = client_factory or BoxNetworkClient
        self.local = threading.local()
        self.clients, self.clients_lock = [], threading.Lock()

    def _client(self, session=None):
        if not hasattr(self.local, 'client'):
            self.local.client = self.client_factory()
            if session is not None and session.proxy_url:
                self.local.client.requests_session.proxies = {
                    'http': session.proxy_url, 'https': session.proxy_url}
            with self.clients_lock:
                self.clients.append(self.local.client)
        return self.local.client

    def close(self):
        # Called after all operation workers have joined, not while requests run.
        for client in self.clients:
            session = getattr(client, 'requests_session', None)
            if session is not None:
                session.close()

    def fetch(self, options):
        runtime = self.runtime
        options = copy(options)
        options.network_session = options.network_session.with_retry_strategy(_NoRetry())
        positions, size = _positions(options)
        safe_read = options.method.upper() in ('GET', 'HEAD', 'OPTIONS')
        path = urlsplit(options.url).path
        kind = ('part' if options.method.upper() == 'PUT' and '/upload_sessions/' in path
                else 'payload' if _streams(options) else 'api')
        for attempt in range(self.attempts):
            runtime.check_cancelled()
            runtime.wait_cooldown()
            generation = runtime.cooldown.auth_generation
            try:
                with runtime.connection():
                    runtime.metrics.add(request_attempts=1, attempted_payload_bytes=size,
                                        **{f'{kind}_attempts': 1})
                    response = self._client(options.network_session).fetch(options)
            except (UploadCancelled, UploadPaused):
                raise
            except Exception as exc:
                status, headers = error_response(exc)
                runtime.metrics.add(**{f'status_{status}': 1})
                if status == 429:
                    # Even the final failure must pause other workers.
                    delay = retry_after(headers)
                    delay = delay if delay is not None else min(30, 2 ** attempt) + random.uniform(0, 0.25)
                    runtime.cooldown.defer(delay, runtime.clock)
                elif status == 401 and options.auth is not None and attempt == 0:
                    with runtime.cooldown.refresh_lock:
                        if generation == runtime.cooldown.auth_generation:
                            options.auth.refresh_token(network_session=options.network_session)
                            runtime.cooldown.auth_generation += 1
                elif safe_read and status != 409 and uncertain_request(exc):
                    if attempt + 1 == self.attempts:
                        raise
                    delay = retry_after(headers)
                    runtime.sleep(delay if delay is not None else min(30, 2 ** attempt) + random.uniform(0, 0.25))
                else:
                    # PUT parts/POST creates/versions/commits need their own outcome
                    # reconciliation. Never repeat uncertain writes here.
                    raise
                if attempt + 1 == self.attempts or positions is None:
                    raise
                for stream, position in positions:
                    stream.seek(position)
                runtime.metrics.add(retried_requests=1)
                continue
            runtime.metrics.add(**{f'status_{response.status}': 1})
            # A part receipt is checked by the coordinator before it is counted.
            if kind == 'payload' and response.status in (200, 201):
                runtime.metrics.add(acknowledged_bytes=size)
            return response
        raise AssertionError('Retry loop exhausted without an outcome')


def prepare_upload_client(client, runtime):
    """Clone a real SDK client, retaining its auth/base URLs; leave test doubles alone."""
    from box_sdk_gen import BoxClient
    if not isinstance(client, BoxClient):
        return client, None
    network = UploadNetworkClient(runtime)
    session = client.network_session.with_network_client(network).with_retry_strategy(_NoRetry())
    result = BoxClient(client.auth, network_session=session)
    try:
        runtime.bind_actor(result.users.get_user_me(fields=['id']).id)
    except BaseException:
        network.close()
        raise
    return result, network
