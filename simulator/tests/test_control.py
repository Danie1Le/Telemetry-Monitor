import json
import urllib.error
import urllib.request

import pytest
from test_run import START, Driver

from simulator.contracts import load_sensors
from simulator.control import serve
from simulator.flight import flight_seconds

POSTED_AT = START + 5


@pytest.fixture
def cfg():
    return load_sensors() | {"engineCount": 2}


@pytest.fixture
def server(cfg):
    server, queue = serve(cfg, 0)
    yield f"http://127.0.0.1:{server.server_address[1]}", queue
    server.shutdown()


def request(url, body=None):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data if body is not None else None)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


FAULT = {
    "kind": "corrupt_unit",
    "engine": 1,
    "sensor": "chamber_temp",
    "startSecond": 0,
    "durationSeconds": 3,
}


def test_healthz_is_200(server):
    assert request(server[0] + "/healthz") == 200


def test_valid_fault_is_accepted_and_queued(server):
    assert request(server[0] + "/faults", FAULT) == 202
    assert server[1].get_nowait() == FAULT


@pytest.mark.parametrize(
    "body",
    [
        FAULT | {"kind": "melt"},
        FAULT | {"extra": 1},
        {k: v for k, v in FAULT.items() if k != "durationSeconds"},
        FAULT | {"sensor": "oxygen"},
        FAULT | {"engine": 3},
        b"not json",
        [FAULT],
    ],
)
def test_invalid_fault_is_400_and_not_queued(server, body):
    assert request(server[0] + "/faults", body) == 400
    assert server[1].empty()


@pytest.mark.parametrize("extra", [{}, {"startSecond": 178}])
def test_start_second_is_optional_and_ignored(server, extra):
    body = {k: v for k, v in FAULT.items() if k != "startSecond"} | extra
    assert request(server[0] + "/faults", body) == 202
    assert server[1].get_nowait()["startSecond"] == 0


def test_400_body_is_a_short_message(server):
    req = urllib.request.Request(
        server[0] + "/faults", json.dumps(FAULT | {"kind": "melt"}).encode()
    )
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    msg = e.value.read().decode()
    assert "melt" in msg
    assert "\n" not in msg


def test_posted_fault_applies_from_the_next_second_in_the_current_flight_only(
    cfg, server
):
    url, queue = server
    cycle = flight_seconds(cfg) + cfg["idleSecondsBetweenFlights"]

    class PostingDriver(Driver):
        def send(self, batch):
            if batch["second"] == POSTED_AT and batch["engineId"] == "engine-1":
                assert request(url + "/faults", FAULT) == 202
            super().send(batch)

    clean = Driver(2 * cycle).run(cfg)
    sent = PostingDriver(2 * cycle).run(cfg, control=queue)
    changed = [c["second"] - START for c, s in zip(clean, sent, strict=True) if c != s]
    assert changed == [6, 7, 8]
    assert {b["flightId"] for b in sent} != {sent[0]["flightId"]}
