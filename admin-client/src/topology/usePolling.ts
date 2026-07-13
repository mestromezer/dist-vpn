import { useEffect, useRef, useState } from 'react';

export interface PollingState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/**
 * Generic "fetch now, then again every intervalMs" hook. This is the interim
 * substitute for Platform's WebSocket `/events` push channel (TZ-6 §2 lists
 * native WebSocket as the intended real-time transport) — that endpoint
 * doesn't exist on the Platform side yet, so polling is what TopologyPage
 * uses for this slice. See README.md TODO for the follow-up.
 *
 * Keeps the last successful `data` on screen if a subsequent poll fails
 * (surfaces `error` alongside it) rather than clearing the graph/list on a
 * transient network blip.
 */
export function usePolling<T>(fetcher: () => Promise<T>, intervalMs: number): PollingState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  useEffect(() => {
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
  }, [intervalMs]);

  return { data, error, loading };
}
