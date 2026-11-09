"""Entry point: poll the queue, store each message's readings, record its alerts, then
delete it."""

import json
import logging
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from jsonschema import ValidationError

from processor import alerts, readings
from processor.contracts import load_sensors

log = logging.getLogger("processor")

# Liveness fails if the loop has not polled for this long; a long poll takes up to 20s.
STALE_AFTER_SECONDS = 60
last_poll = time.monotonic()


def handle(sqs, queue_url: str, dlq_url: str, message: dict) -> None:
    """Store a message and record its alerts, or dead-letter it now if it can never be
    stored. Then delete it.

    Any other error propagates and the message stays on the queue, so SQS redelivers it
    and the redrive policy moves it to the DLQ after 3 receives.
    """
    try:
        body = json.loads(message["Body"])
        readings.store(body, int(time.time()))
        alerts.record(body)
    except (json.JSONDecodeError, ValidationError) as e:
        log.warning("message %s dead-lettered: %s", message["MessageId"], e)
        sqs.send_message(QueueUrl=dlq_url, MessageBody=message["Body"])
    sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])


def poll(sqs, queue_url: str, dlq_url: str, wait_seconds: int = 20) -> None:
    """One receive of up to 10 messages, each handled on its own."""
    global last_poll
    response = sqs.receive_message(
        QueueUrl=queue_url, MaxNumberOfMessages=10, WaitTimeSeconds=wait_seconds
    )
    last_poll = time.monotonic()
    for message in response.get("Messages", []):
        try:
            handle(sqs, queue_url, dlq_url, message)
        except Exception:
            log.exception("message %s left for redelivery", message["MessageId"])


class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        alive = time.monotonic() - last_poll < STALE_AFTER_SECONDS
        self.send_response(200 if alive else 503)
        self.end_headers()

    def log_message(self, *args):  # probes would flood the log
        pass


def main() -> None:  # pragma: no cover - wiring, run by the Testcontainers tests
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    # A bad config must stop startup, not dead-letter every message.
    load_sensors()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    server = ThreadingHTTPServer(("", 8080), Health)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sqs = boto3.client("sqs")
    queue_url, dlq_url = os.environ["QUEUE_URL"], os.environ["DLQ_URL"]
    while not stop.is_set():  # SIGTERM lets the current batch finish
        poll(sqs, queue_url, dlq_url)


if __name__ == "__main__":
    main()
