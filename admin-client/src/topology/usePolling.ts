import { useEffect, useRef, useState } from 'react';

export interface PollingState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

export interface UsePollingOptions {
  /**
   * When `false`, pauses scheduling any *further* poll — the in-flight
   * request (if any) still resolves, but no new `setTimeout(tick, ...)` is
   * queued after it, and the existing `data`/`error` are left exactly as
   * they were (not cleared). Flipping back to `true` resumes polling
   * immediately (fires a fresh fetch right away, not after a full
   * `intervalMs` wait).
   *
   * Defaults to `true` (matches this hook's original always-on behavior).
   *
   * Added this slice for `topology/liveTopology.ts`'s WS-primary /
   * polling-fallback hybrid: once the `/events` WebSocket has proven itself
   * `open`, there's no need to keep re-fetching the full snapshot on a
   * timer — live deltas arrive over the socket instead — but polling needs
   * to resume without a page reload if the socket later drops. The very
   * first render always starts with `enabled: true` in practice (the socket
   * begins in `connecting`, never synchronously `open` — see
   * eventsSocket.ts's `stableAfterMs`), so the initial full snapshot is
   * always fetched over REST at least once regardless of how quickly the
   * socket later opens.
   */
  enabled?: boolean;
}

/**
 * Generic "fetch now, then again every intervalMs" hook. Originally the sole
 * data source for TopologyPage (interim substitute for Platform's
 * WebSocket `/events` push channel, which didn't exist yet); now also reused
 * as the polling-fallback half of `topology/liveTopology.ts`'s WS-primary
 * hook once `/events` is real (see that file, and admin-client/README.md).
 *
 * Keeps the last successful `data` on screen if a subsequent poll fails
 * (surfaces `error` alongside it) rather than clearing the graph/list on a
 * transient network blip.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  intervalMs: number,
  options: UsePollingOptions = {},
): PollingState<T> {
  const { enabled = true } = options;
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const tick = async () => {
      try {
        const result = await fetcherRef.current();
        if (cancelled) return;
        setData(result);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load data');
      } finally {
        if (!cancelled) {
          setLoading(false);
          timer = setTimeout(tick, intervalMs);
        }
      }
    };

    void tick();

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [intervalMs, enabled]);

  return { data, error, loading };
}
