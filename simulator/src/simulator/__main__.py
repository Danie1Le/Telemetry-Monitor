"""Entry point: run flights back to back, one batch per engine per second."""

import http.client
import json
import logging
import os
import random
import time
import urllib.request
from functools import partial
from pathlib import Path

from simulator.contracts import load_sensors
from simulator.control import serve
from simulator.faults import apply_faults, load_scenario
from simulator.flight import check_config, flight_seconds, make_batch, start_flight

log = logging.getLogger("simulator")


def post(url: str, batch: dict) -> None:
    request = urllib.request.Request(
        url, json.dumps(batch).encode(), {"Content-Type": "application/json"}
    )
    urllib.request.urlopen(request, timeout=2).close()


def run(
    cfg, rng, send, clock=time.time, sleep=time.sleep, faults=(), control=None
) -> None:
    """Loops forever; clock, sleep and send are injected so tests need no real time.

    faults replay on every flight: startSecond counts from each flight's start, and no fault
    is ever removed, so scenario runs are repeatable. Faults taken from the control queue
    are one-shot: each starts at the next second and is dropped when its flight ends, so one
    posted while no flight is running (the idle gap) is lost.
    """
    flight = None
    live = []
    while True:
        second = int(clock())
        cycle = flight_seconds(cfg) + cfg["idleSecondsBetweenFlights"]
        # drained before the rollover below, so a fault posted in a flight never joins the next
        while flight and control and not control.empty():
            live.append(control.get() | {"startSecond": second - flight.start})
        if flight is None or second >= flight.start + cycle:
            flight = start_flight(second, rng)
            live = []
            log.info("flight %s started", flight.flight_id)
        for engine in range(1, cfg["engineCount"] + 1):
            batch = make_batch(cfg, flight, engine, second)
            if batch is None:  # idle between flights
                continue
            try:
                send(apply_faults(cfg, [*faults, *live], flight, batch))
            except (OSError, http.client.HTTPException):
                # shortcut: no retry, the batch is dropped; add a bounded retry if ingest outages matter.
                log.warning(
                    "send failed: engine-%d second %d", engine, second, exc_info=True
                )
        # slow sends: skip missed seconds, never catch up
        sleep(max(0, second + 1 - clock()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    cfg = load_sensors()
    check_config(cfg)
    url = os.environ.get("INGEST_URL", "http://localhost:8080/readings")
    scenario = os.environ.get("SCENARIO")
    faults = load_scenario(Path(scenario), cfg) if scenario else []
    _, control = serve(cfg, int(os.environ.get("CONTROL_PORT", "8081")))
    run(
        cfg,
        random.Random(os.environ.get("SEED") or None),
        partial(post, url),
        faults=faults,
        control=control,
    )


if __name__ == "__main__":
    main()
