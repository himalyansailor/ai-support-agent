from supportworker.metrics import QUEUE_NAMES, QueueDepthCollector


class FakeRedis:
    def __init__(self, depths: dict[str, int] | None = None, error: Exception | None = None) -> None:
        self.depths, self.error = depths or {}, error

    def llen(self, name: str) -> int:
        if self.error:
            raise self.error
        return self.depths.get(name, 0)


def _samples(collector) -> dict[str, float]:
    return {s.labels["queue"]: s.value for family in collector.collect() for s in family.samples}


def test_queue_depth_collector():
    collector = QueueDepthCollector(lambda: FakeRedis({"celery": 7}))
    assert QUEUE_NAMES == ("celery",)
    assert _samples(collector) == {"celery": 7.0}
    names = [family.name for family in collector.collect()]
    assert names == ["support_job_queue_depth"]


def test_queue_depth_timeout_gives_no_sample():
    collector = QueueDepthCollector(lambda: FakeRedis(error=TimeoutError("redis timed out")))
    assert _samples(collector) == {}


def test_queue_depth_unreachable_factory_gives_no_sample():
    def boom():
        raise ConnectionError("down")

    assert _samples(QueueDepthCollector(boom)) == {}
