// Constant RATE batches/s for DURATION (a k6 duration such as 10m). Fails on any non-2xx
// response (5xx included), any batch with a rejected reading, or any batch k6 could not send
// on time (the rate was not held).
import { postBatch } from './batch.js';

export { setup } from './batch.js';

export const options = {
  scenarios: {
    sustained: {
      executor: 'constant-arrival-rate',
      rate: Number(__ENV.RATE),
      timeUnit: '1s',
      duration: __ENV.DURATION,
      preAllocatedVUs: Number(__ENV.RATE),
      maxVUs: 10 * Number(__ENV.RATE), // rides out ~10 s latency stalls without dropping batches
    },
  },
  thresholds: {
    http_req_failed: ['rate==0'],
    checks: ['rate==1'],
    dropped_iterations: ['count==0'],
  },
};

export default postBatch;
