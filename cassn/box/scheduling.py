"""Bounded file-job scheduling with room for both small files and recordings."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED


def upload_results(paths, upload, *, cancel, is_large, workers=8, pending_limit=32, metrics=None):
    """Yield (path, future), retaining at most pending_limit jobs.

    While small files remain, at most two large file coordinators are submitted.
    Their part requests use a separate executor and the shared network budget.
    When one class runs out the other borrows its capacity.
    """
    if workers < 1 or pending_limit < workers:
        raise ValueError('Pending upload budget must cover all file workers')
    small, large = deque(), deque()
    for path in paths:
        (large if is_large(path) else small).append(path)
    pending = {}
    large_count = 0
    large_limit = max(1, min(2, workers - 1))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='box-file') as pool:
        while pending or small or large:
            while len(pending) < pending_limit and (small or large) and not cancel.is_set():
                choose_large = bool(large and (not small or large_count < large_limit))
                path = (large if choose_large else small).popleft()
                future = pool.submit(upload, path)
                pending[future] = (path, choose_large)
                large_count += int(choose_large)
                if metrics:
                    metrics.add(pending_jobs=1)
            if cancel.is_set():
                for future in pending:
                    future.cancel()
            if not pending:
                break
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                path, was_large = pending.pop(future)
                large_count -= int(was_large)
                if metrics:
                    metrics.add(pending_jobs=-1)
                if not future.cancelled():
                    yield path, future
