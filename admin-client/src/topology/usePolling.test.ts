import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { usePolling } from './usePolling';

// Real timers throughout (matches TestRequestButton.test.tsx's own polling
// test style) rather than vi.useFakeTimers() -- usePolling's tick() is async
// (awaits the fetcher's promise), and mixing fake timers with
// @testing-library's waitFor (which itself polls via setTimeout) is a known
// source of flaky/hanging tests unless carefully advanced in lockstep. A
// short real interval (30ms) keeps this file fast without that risk.
const INTERVAL_MS = 30;

/**
 * Covers the `enabled` option added this slice for
 * topology/liveTopology.ts's WS-primary/polling-fallback hybrid -- the
 * hook's original always-on behavior (no `options` argument at all) is
 * already exercised indirectly via TopologyPage.test.tsx, so this file
 * focuses on the pause/resume contract specifically.
 */
describe('usePolling enabled option', () => {
  it('polls normally when enabled is omitted (default true)', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    const { result } = renderHook(() => usePolling(fetcher, INTERVAL_MS));

    await waitFor(() => expect(result.current.data).toBe('v1'));
    await waitFor(() => expect(fetcher.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it('never fetches while enabled starts false', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    const { result } = renderHook(({ enabled }) => usePolling(fetcher, INTERVAL_MS, { enabled }), {
      initialProps: { enabled: false },
    });

    // Give it several would-be intervals' worth of real time to prove
    // silence, not just an instant after mount.
    await new Promise((resolve) => setTimeout(resolve, INTERVAL_MS * 3));
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.data).toBeNull();
  });

  it('pauses scheduling further polls when enabled flips to false, keeping the last data', async () => {
    const fetcher = vi.fn().mockResolvedValue('v1');
    const { result, rerender } = renderHook(
      ({ enabled }) => usePolling(fetcher, INTERVAL_MS, { enabled }),
      { initialProps: { enabled: true } },
    );

    await waitFor(() => expect(result.current.data).toBe('v1'));
    rerender({ enabled: false });
    const callsAtPause = fetcher.mock.calls.length;

    // No further calls no matter how much (real) time passes while paused.
    await new Promise((resolve) => setTimeout(resolve, INTERVAL_MS * 5));
    expect(fetcher.mock.calls.length).toBe(callsAtPause);
    // Last successful data is retained, not cleared, while paused.
    expect(result.current.data).toBe('v1');
  });

  it('resumes polling immediately (not after a full interval wait) when enabled flips back to true', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce('v1').mockResolvedValue('v2');
    // Deliberately long interval so "resumes immediately" can't be
    // mistaken for "happened to line up with the next scheduled tick".
    const longInterval = INTERVAL_MS * 20;
    const { result, rerender } = renderHook(
      ({ enabled }) => usePolling(fetcher, longInterval, { enabled }),
      { initialProps: { enabled: true } },
    );

    await waitFor(() => expect(result.current.data).toBe('v1'));
    rerender({ enabled: false });

    rerender({ enabled: true });
    await waitFor(() => expect(result.current.data).toBe('v2'), { timeout: longInterval });
  });
});
