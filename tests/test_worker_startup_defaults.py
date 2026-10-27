from app import main


class _UnexpectedThread:
    def __init__(self, *args, **kwargs):
        raise AssertionError("artifact delivery worker must remain disabled by default")


class _StartedThread:
    starts = 0

    def __init__(self, *args, **kwargs):
        self.name = kwargs["name"]

    def start(self):
        type(self).starts += 1


def test_artifact_delivery_worker_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ARTIFACT_DELIVERY_WORKER_ENABLED", raising=False)
    monkeypatch.setattr(main, "_delivery_worker_started", False)
    monkeypatch.setattr(main.threading, "Thread", _UnexpectedThread)

    main.start_delivery_worker()

    assert main._delivery_worker_started is False


def test_artifact_delivery_worker_requires_explicit_enablement(monkeypatch):
    monkeypatch.setenv("ARTIFACT_DELIVERY_WORKER_ENABLED", "true")
    monkeypatch.setenv("ARTIFACT_DELIVERY_WORKERS", "2")
    monkeypatch.setattr(main, "_delivery_worker_started", False)
    monkeypatch.setattr(main.threading, "Thread", _StartedThread)
    _StartedThread.starts = 0

    main.start_delivery_worker()

    assert _StartedThread.starts == 2
    assert main._delivery_worker_started is True
