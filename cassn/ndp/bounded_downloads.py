"""Bounded, killable Box source workers with fresh parent-owned progress."""
from __future__ import annotations

import multiprocessing
import os
import queue
import time


class SourceBatchFailed(Exception):
    def __init__(self, detail):
        self.detail = detail
        super().__init__("Source workers stopped; see safe failure detail")


def _worker(function, tasks, results, stop):
    while not stop.is_set():
        try:
            item = tasks.get(timeout=.2)
        except queue.Empty:
            continue
        if item is None:
            return
        number, payload = item
        if stop.is_set():
            return
        try:
            result = function(payload)
        except Exception as error:
            number_errno = getattr(error, 'errno', None)
            result = dict(ok=False, error_type=type(error).__name__,
                          errno=number_errno,
                          message=os.strerror(number_errno) if isinstance(number_errno, int)
                          and number_errno > 0 else "Remote exception text omitted")
        results.put((number, result))


def run_bounded(function, files, *, workers=3, file_timeout=120, drain_timeout=10,
                report=lambda event: None, cancel=None, heartbeat=2):
    """Schedule only one worker window; stop scheduling at the first failure.

    Child processes own network reads, so a blocked SDK stream cannot prevent
    the source owner from reporting or enforcing its drain deadline. No WI
    process is created, signalled or authenticated by this executor.
    """
    if type(workers) is not int or not 1 <= workers <= 3:
        raise ValueError('At most three source workers')
    if min(file_timeout, drain_timeout, heartbeat) <= 0:
        raise ValueError('Source deadlines must be positive')
    context = multiprocessing.get_context('spawn')
    tasks = context.Queue(maxsize=workers)
    results = context.Queue(maxsize=workers)
    stop = context.Event()
    children = [context.Process(target=_worker, args=(function, tasks, results, stop),
                                daemon=True) for _ in range(workers)]
    pending = {}
    iterator = iter(enumerate(files))
    exhausted = False
    failure = None
    deadline = None
    completed = 0
    last_success = None
    last_report = 0

    def emit(state):
        nonlocal last_report
        report(dict(state=state, completed=completed, last_success=last_success,
                    active=[dict(filename=item['filename'],
                                 elapsed_seconds=round(time.monotonic()-started, 3))
                            for item, started in pending.values()],
                    failure=failure, worker_pids=[p.pid for p in children]))
        last_report = time.monotonic()

    def fail(detail):
        nonlocal failure, deadline
        if failure is None:
            failure = detail
            deadline = time.monotonic() + drain_timeout
            stop.set()
            emit('draining_after_failure')

    try:
        for child in children:
            child.start()
        while pending or not exhausted:
            if failure is None and cancel is not None and cancel.is_set():
                fail(dict(error_type='SourceCancelled', operation='cancel'))
            while failure is None and len(pending) < workers and not exhausted:
                item = next(iterator, None)
                if item is None:
                    exhausted = True
                    break
                number, payload = item
                tasks.put((number, payload), timeout=1)
                pending[number] = (payload, time.monotonic())
            if time.monotonic() - last_report >= heartbeat:
                emit('draining_after_failure' if failure else 'working')
            if failure and (not pending or time.monotonic() >= deadline):
                break
            if not pending:
                break
            try:
                number, result = results.get(timeout=min(.2, heartbeat))
            except queue.Empty:
                for payload, started in pending.values():
                    if time.monotonic()-started >= file_timeout:
                        fail(dict(error_type='SourceFileTimeout', operation='download_or_validate',
                                  filename=payload['filename']))
                        break
                if any(child.exitcode is not None for child in children) and failure is None:
                    fail(dict(error_type='SourceWorkerExited', operation='worker'))
                continue
            payload, _ = pending.pop(number)
            if not isinstance(result, dict) or result.get('ok') is not True:
                detail = result if isinstance(result, dict) else dict(error_type='InvalidWorkerResult')
                fail(dict(detail, filename=payload['filename']))
            else:
                completed += 1
                last_success = dict(filename=payload['filename'], finished_epoch=time.time(),
                                    skipped=bool(result.get('skipped')))
                emit('draining_after_failure' if failure else 'working')
        if failure:
            raise SourceBatchFailed(dict(failure, completed=completed,
                                         unfinished=[x[0]['filename'] for x in pending.values()]))
        emit('complete')
        return completed
    finally:
        stop.set()
        # These handles belong exclusively to this source executor. Never use
        # a global process search or signal the WI owner to stop a Box read.
        for child in children:
            if child.pid is None:
                continue
            child.join(timeout=.2)
            if child.is_alive():
                child.terminate()
                child.join(timeout=2)
            if child.is_alive():
                child.kill()
                child.join(timeout=2)
        for channel in (tasks, results):
            channel.cancel_join_thread()
            channel.close()


_box_client = None


def download_source_file(payload):
    """A child owns its Box client and one atomic, hash-verified file at a time."""
    from pathlib import Path
    from cassn.box.auth import get_box_client, load_box_config
    from cassn.ndp.box_download import download_box_file
    from cassn.ndp.transfer import TransferFile
    global _box_client
    if _box_client is None:
        _box_client = get_box_client(load_box_config())
        if _box_client is None:
            return dict(ok=False, error_type='BoxClientUnavailable', operation='connect')
    root = Path(payload['root']).resolve()
    destination = root / payload['relative_path']
    if destination.resolve() != destination or not destination.is_relative_to(root):
        return dict(ok=False, error_type='SourcePathChanged', operation='resolve')
    source = TransferFile(payload['file_id'], payload['filename'], payload['relative_path'],
                          payload['size'], payload['sha256'], payload['sha1'])
    try:
        result = download_box_file(_box_client, source, destination)
        return dict(ok=True, skipped=result.skipped, bytes_written=result.bytes_written)
    except Exception as error:
        context = getattr(error, 'box_download_context', None)
        return dict(ok=False, **(context or dict(error_type=type(error).__name__,
                                                operation='download_or_verify',
                                                errno=getattr(error, 'errno', None))))
