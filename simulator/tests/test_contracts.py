import json

from simulator.contracts import CONTRACTS_DIR, load_sensors, validator


def test_example_matches_schema():
    example = json.loads((CONTRACTS_DIR / "http-batch.example.json").read_text())
    validator("http-batch").validate(example)


def test_sensors_yaml_matches_schema():
    assert load_sensors()["schemaVersion"] == 1
