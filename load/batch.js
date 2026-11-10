// Posts one valid batch (contracts/http-batch.schema.json) per iteration: engine-second batches
// of every sensor in sensors.yaml at rateHz, in the ascent phase, with fresh timestamps.
// Run through scripts/measure.py, which passes BASE_URL, SENSORS (sensors.yaml as JSON),
// RATE (batches/s, also the engine count, so each engine posts about once per second),
// FLIGHT_ID and, for a chaos run, SPIKE_AT: seconds after the start at which every engine's
// chamber temperature spikes for SPIKE_SECONDS (no spike when unset).
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const cfg = JSON.parse(__ENV.SENSORS);
const PHASE = 'ascent';
// Engines from 1001 up, so load never collides with the simulator's engine-1.. batches,
// whose readings share the `readings` table key (sensorId, second).
const FIRST_ENGINE = 1001;
// The spiked sensor and its alerts must match EXPECTED["spike"] in scripts/e2e.py.
const SPIKE_SENSOR = 'chamber_temp';
const SPIKE_SECONDS = 3;

export function setup() {
  return { flightId: __ENV.FLIGHT_ID, start: Math.floor(Date.now() / 1000) };
}

export function postBatch(data) {
  const engineId = `engine-${FIRST_ENGINE + (exec.scenario.iterationInTest % Number(__ENV.RATE))}`;
  const second = Math.floor(Date.now() / 1000);
  // From the batch second, not the clock, so a repeated (engine, second) batch is spiked alike.
  const at = second - data.start - Number(__ENV.SPIKE_AT);
  const spike = at >= 0 && at < SPIKE_SECONDS;
  const readings = [];
  for (const [type, spec] of Object.entries(cfg.sensorTypes)) {
    const lim = spec.phases[PHASE];
    const base = spike && type === SPIKE_SENSOR ? lim.max + 10 * lim.noise : lim.nominal;
    for (let k = 0; k < cfg.rateHz; k++) {
      readings.push({
        sensorId: `${engineId}.${type}`,
        ts: second * 1000 + Math.floor((k * 1000) / cfg.rateHz),
        // Uniform noise like the simulator: never flat, never drifting, never too fast.
        value: base + (Math.random() * 2 - 1) * lim.noise,
        unit: spec.unit,
      });
    }
  }
  const batch = {
    schemaVersion: cfg.schemaVersion,
    flightId: data.flightId,
    phase: PHASE,
    engineId,
    second,
    readings,
  };
  // Metadata, not tags: per-request values would make a time series per request.
  const meta = exec.vu.metrics.metadata;
  Object.assign(meta, { engineId, second: String(second), spike: String(spike) });
  const res = http.post(`${__ENV.BASE_URL}/readings`, JSON.stringify(batch), {
    headers: { 'Content-Type': 'application/json' },
  });
  check(res, {
    'batch accepted': (r) => r.status === 202 && r.json('rejected') === 0,
  });
}
