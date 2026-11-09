import json

import pytest

from processor.contracts import CONTRACTS_DIR, load_sensors, validator


@pytest.mark.parametrize("name", ["sqs-message", "alert"])
def test_example_matches_schema(name):
    example = json.loads((CONTRACTS_DIR / f"{name}.example.json").read_text())
    validator(name).validate(example)


def test_sensors_yaml_matches_schema():
    assert load_sensors()["schemaVersion"] == 1
