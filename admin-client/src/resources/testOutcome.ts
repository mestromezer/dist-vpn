import type { LinkTest, TestOutcome } from '../contract/types';

/**
 * Maps LinkTest.outcome to a human-readable sentence, per TZ-6 §3.2 ("не как
 * голый JSON, а как понятный человеку статус: успех с задержкой, либо явное
 * указание этапа отказа"). Kept as a pure function (no React, no fetch) so it
 * has a plain unit test independent of the polling/button UI around it.
 *
 * The contract's LinkTest.detail is documented as already being a
 * human-readable explanation from the server, but this slice does not rely
 * on the server phrasing alone — Platform doesn't exist yet to verify its
 * wording is consistent, and TZ-6 explicitly asks the client to map each
 * enum value to a clear sentence itself. When present, `detail` is appended
 * as extra context rather than substituted in.
 */
const OUTCOME_MESSAGES: Record<TestOutcome, string> = {
  success: 'Connection test succeeded.',
  container_start_failed: 'Failed: could not start the test client container on the entry node.',
  tls_handshake_failed: 'Failed: the TLS handshake did not complete.',
  proxy_no_response: 'Failed: the proxy did not respond in time.',
  timeout: 'Failed: the test request timed out.',
};

export function describeTestOutcome(result: LinkTest): string {
  if (!result.outcome) {
    return 'Test is still running…';
  }
  const message = OUTCOME_MESSAGES[result.outcome] ?? `Unknown result: ${result.outcome}`;
  const parts = [message];
  if (result.outcome === 'success' && typeof result.latency_ms === 'number') {
    parts.push(`Round-trip latency: ${result.latency_ms} ms.`);
  }
  if (result.detail) {
    parts.push(`(${result.detail})`);
  }
  return parts.join(' ');
}

/** True once the test has a terminal outcome (success or a specific failure). */
export function isTestFinished(result: LinkTest): boolean {
  return Boolean(result.outcome);
}
