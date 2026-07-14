import { describe, expect, it } from 'vitest';
import type { LinkTest, TestOutcome } from '../contract/types';
import { describeTestOutcome, isTestFinished } from './testOutcome';

function linkTest(overrides: Partial<LinkTest> = {}): LinkTest {
  return { id: 't1', cascade_id: 'c1', ...overrides };
}

describe('describeTestOutcome', () => {
  it('reports success with round-trip latency', () => {
    const text = describeTestOutcome(linkTest({ outcome: 'success', latency_ms: 42 }));
    expect(text).toMatch(/succeeded/i);
    expect(text).toMatch(/42 ms/);
  });

  it.each<[TestOutcome, RegExp]>([
    ['container_start_failed', /start.*container/i],
    ['tls_handshake_failed', /TLS handshake/i],
    ['proxy_no_response', /proxy did not respond/i],
    ['timeout', /timed out/i],
  ])('maps outcome %s to a clear, non-JSON sentence', (outcome, expected) => {
    const text = describeTestOutcome(linkTest({ outcome }));
    expect(text).toMatch(expected);
    expect(text).toMatch(/^Failed:/);
    // Never leak the raw enum value as the primary message.
    expect(text.startsWith(outcome)).toBe(false);
  });

  it('appends server-provided detail as extra context when present', () => {
    const text = describeTestOutcome(
      linkTest({ outcome: 'tls_handshake_failed', detail: 'certificate expired' }),
    );
    expect(text).toContain('certificate expired');
  });

  it('reports "still running" while outcome is absent (test still in flight)', () => {
    expect(describeTestOutcome(linkTest())).toMatch(/still running/i);
    expect(isTestFinished(linkTest())).toBe(false);
    expect(isTestFinished(linkTest({ outcome: 'success' }))).toBe(true);
  });
});
