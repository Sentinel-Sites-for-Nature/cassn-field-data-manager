import threading
from cassn.box.scheduling import upload_results
from cassn.box.transfer import UploadMetrics


def test_large_queue_is_bounded_and_completes_each_file_once():
    peaks=[]
    metrics=UploadMetrics(callback=lambda s:peaks.append(s.get('pending_jobs',0)))
    cancel=threading.Event()
    results=list(upload_results(range(20000), lambda n:n, cancel=cancel,
                                is_large=lambda n:False, metrics=metrics))
    assert {f.result() for _,f in results}==set(range(20000))
    assert metrics.snapshot()['pending_jobs']==0
    assert metrics.snapshot()['peak_pending_jobs']<=32
    assert max(peaks)<=32


def test_both_classes_start_without_large_files_occupying_every_worker():
    cancel=threading.Event()
    small_started=threading.Event()
    counts={'large':0,'small':0}
    lock=threading.Lock()
    def work(n):
        with lock: counts['large' if n<10 else 'small']+=1
        if n<10:
            assert small_started.wait(2), 'large coordinators starved photos'
        else: small_started.set()
        return n
    results=list(upload_results(range(110), work, cancel=cancel, is_large=lambda n:n<10))
    assert len(results)==110
    assert counts=={'large':10,'small':100}
    assert all(f.result()==n for n,f in results)


def test_cancel_stops_refill_and_does_not_report_queued_work_as_completed():
    cancel=threading.Event()
    def work(n):
        cancel.set()
        return n
    results=list(upload_results(range(10000),work,cancel=cancel,is_large=lambda n:False))
    assert 1<=len(results)<=32
    assert len(results)<10000


def test_failures_do_not_drop_later_files():
    def work(n):
        if n==5: raise RuntimeError('test')
        return n
    results=list(upload_results(range(100),work,cancel=threading.Event(),is_large=lambda n:False))
    assert len(results)==100
    assert [n for n,f in results if f.exception()]==[5]
