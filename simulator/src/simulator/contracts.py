import json
import os
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

# Default is the repo layout; in the cluster the files are mounted and these env vars point at them.
_REPO = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = Path(os.environ.get("CONTRACTS_DIR", _REPO / "contracts"))
SENSORS_CONFIG = Path(os.environ.get("SENSORS_CONFIG", _REPO / "sensors.yaml"))


def validator(name: str) -> Draft202012Validator:
    """Validator for contracts/<name>.schema.json; relative $refs resolve within CONTRACTS_DIR."""
    registry = Registry().with_resources(
        (p.name, Resource.from_contents(json.loads(p.read_text())))
        for p in CONTRACTS_DIR.glob("*.schema.json")
    )
    schema = json.loads((CONTRACTS_DIR / f"{name}.schema.json").read_text())
    return Draft202012Validator(schema, registry=registry)


def load_yaml(path: Path, schema: str) -> dict:
    """Parse a YAML file and validate it against contracts/<schema>.schema.json."""
    data = yaml.safe_load(path.read_text())
    validator(schema).validate(data)
    return data


def load_sensors() -> dict:
    return load_yaml(SENSORS_CONFIG, "sensors")
