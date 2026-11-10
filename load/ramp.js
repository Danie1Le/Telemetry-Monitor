// Stepped ramp: STEPS (comma-separated batches/s) held STEP_SECONDS each, one scenario per
// step, so the summary reports each step's p95 and failure rate on its own. A step fails its
// threshold on any non-2xx response (5xx included), rejected reading or dropped batch. A batch is 50 readings (5 sensors x 10).
import { postBatch } from './batch.js';

export { setup } from './batch.js';

const steps = __ENV.STEPS.split(',').map(Number);
const seconds = Number(__ENV.STEP_SECONDS);

export const options = {
  scenarios: Object.fromEntries(
    steps.map((rate, i) => [
      `rate_${rate}`,
      {
        executor: 'constant-arrival-rate',
        rate,
        timeUnit: '1s',
        duration: `${seconds}s`,
        startTime: `${i * seconds}s`,
        preAllocatedVUs: rate,
        maxVUs: 10 * rate, // rides out ~10 s latency stalls without dropping batches
        env: { RATE: String(rate) },
      },
    ]),
  ),
  thresholds: Object.fromEntries(
    steps.flatMap((rate) => [
      [`http_req_failed{scenario:rate_${rate}}`, ['rate==0']],
      // Always passes: listed so the summary export includes each step's latency.
      [`http_req_duration{scenario:rate_${rate}}`, ['p(95)>=0']],
      [`dropped_iterations{scenario:rate_${rate}}`, ['count==0']],
      [`checks{scenario:rate_${rate}}`, ['rate==1']],
    ]),
  ),
};

export default postBatch;
