import pytest
from prometheus_client import REGISTRY

from supportapi.websocket import track_connection


def _open(kind: str) -> float:
    return REGISTRY.get_sample_value("support_ws_connections", {"kind": kind}) or 0.0


def test_track_connection_counts_open_sockets_and_releases_on_error():
    before = _open("inbox")
    with track_connection("inbox"):
        assert _open("inbox") == before + 1
    assert _open("inbox") == before
    with pytest.raises(RuntimeError), track_connection("inbox"):
        raise RuntimeError("socket failed")
    assert _open("inbox") == before
