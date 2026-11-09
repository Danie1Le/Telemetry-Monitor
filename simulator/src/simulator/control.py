"""Live control endpoint: POST /faults queues one fault, GET /healthz answers k8s probes."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import SimpleQueue
from threading import Thread

from jsonschema import ValidationError

from simulator.contracts import validator
from simulator.faults import check_faults


def serve(cfg: dict, port: int) -> tuple[ThreadingHTTPServer, SimpleQueue]:
    """Start the server on a daemon thread; valid faults land in the returned queue.

    The fault has the scenario shape and is checked like a scenario fault. Its startSecond
    is optional and ignored (forced to 0): run() starts the fault at the next second of the
    current flight.
    """
    queue: SimpleQueue = SimpleQueue()
    schema = validator("scenario")

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, body: str = "") -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(body.encode())))
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):
            self.reply(200 if self.path == "/healthz" else 404)

        def do_POST(self):
            if self.path != "/faults":
                return self.reply(404)
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fault = {**body, "startSecond": 0} if isinstance(body, dict) else body
                schema.validate({"faults": [fault]})
                check_faults([fault], cfg, "POST /faults")
            except (ValueError, TypeError, ValidationError) as e:
                return self.reply(400, getattr(e, "message", str(e)))
            queue.put(fault)
            self.reply(202)

    server = ThreadingHTTPServer(("", port), Handler)
    # shortcut: no auth, the port is cluster-internal; add a token if it is ever exposed.
    server.daemon_threads = True
    Thread(target=server.serve_forever, daemon=True).start()
    return server, queue
