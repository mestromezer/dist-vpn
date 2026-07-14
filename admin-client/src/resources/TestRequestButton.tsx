import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, Chip, CircularProgress, Stack } from '@mui/material';
import { useRecordContext } from 'react-admin';
import type { Cascade, LinkTest } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';
import type { RealtimeMessage } from '../realtime/eventsSocket';
import { useEventsSocket } from '../realtime/useEventsSocket';
import { describeTestOutcome, isTestFinished } from './testOutcome';

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 30_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type Status = 'idle' | 'starting' | 'polling' | 'done' | 'error';

/**
 * "Test connection" button for a Cascade (TZ-6 §3.2): POST /cascades/{id}/test,
 * then waits for the result over the shared `/events` WebSocket (a `LinkTest`
 * push matching this test's id) when that connection is `'open'`, falling
 * back to the previous slice's GET /tests/{test_id} polling otherwise — same
 * WS-primary/polling-fallback shape as topology/liveTopology.ts, applied to
 * a single in-flight test instead of the whole topology snapshot. Either
 * way, the outcome renders as human-readable text via describeTestOutcome
 * (never raw JSON/enum values).
 *
 * Reads the Cascade record from context (same pattern as the status Chip
 * fields in nodes.tsx/cascades.tsx) so it can be dropped straight into
 * <SimpleShowLayout> without prop plumbing.
 *
 * **Verification status**: `POST /cascades/{id}/test` and `GET /tests/{id}`
 * are real (not stubbed) on Platform's `platform/api-skeleton` branch as of
 * this slice — see `platform/src/dist_vpn_platform/api/routes/{cascades,
 * tests}.py` and `services/link_tests.py` in the sibling worktree, read
 * directly for this check (an earlier draft of this comment claimed both
 * were still `501`, and that a live click-through had confirmed the 501 —
 * neither is accurate against the current source; corrected here rather
 * than carried forward). What *is* true: no live click-through through an
 * actual running Admin Client + Platform in a browser was performed this
 * slice (only this component's own mocked-fetch tests, plus reading
 * Platform's source directly) — a real end-to-end run, including the
 * WS-push and REST-poll-to-success paths against Platform's real dispatch
 * flow, remains a next-slice TODO. See README.md.
 */
export function TestRequestButton() {
  const record = useRecordContext<Cascade>();
  const [status, setStatus] = useState<Status>('idle');
  const [result, setResult] = useState<LinkTest | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Guards against setState after the component unmounts mid-poll (e.g. the
  // admin navigates away before a slow test resolves).
  const mountedRef = useRef(true);
  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  // The test_id currently being waited on, or null when idle/settled. Also
  // doubles as the "has this already been resolved" guard: both the WS
  // handler and the REST polling loop check it before acting, and whichever
  // resolves first clears it so the other becomes a no-op.
  const pendingTestIdRef = useRef<string | null>(null);

  const handleRealtimeMessage = useCallback((message: RealtimeMessage) => {
    if (message.type !== 'LinkTest') return;
    if (!pendingTestIdRef.current || message.data.id !== pendingTestIdRef.current) return;
    if (!isTestFinished(message.data)) return;
    if (!mountedRef.current) return;
    pendingTestIdRef.current = null;
    setResult(message.data);
    setStatus('done');
  }, []);

  const wsState = useEventsSocket(handleRealtimeMessage);
  const wsStateRef = useRef(wsState);
  wsStateRef.current = wsState;

  if (!record) return null;

  const runTest = async () => {
    setStatus('starting');
    setResult(null);
    setError(null);
    try {
      const { body } = await authorizedFetchJson<{ test_id: string }>(
        `/cascades/${record.id}/test`,
        { method: 'POST' },
      );
      if (!mountedRef.current) return;
      setStatus('polling');
      await pollUntilDone(body.test_id);
    } catch (err) {
      if (!mountedRef.current) return;
      pendingTestIdRef.current = null;
      setStatus('error');
      setError(err instanceof Error ? err.message : 'Failed to start the test request.');
    }
  };

  const pollUntilDone = async (testId: string) => {
    pendingTestIdRef.current = testId;
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      if (!mountedRef.current) return;
      // Resolved already, either by the WS handler above or by a previous
      // loop iteration's REST response — stop without a redundant fetch.
      if (pendingTestIdRef.current !== testId) return;
      // While the WS channel is 'open', trust it and skip the REST call
      // entirely this tick; still re-check every POLL_INTERVAL_MS in case
      // the socket drops mid-wait, so this loop remains the fallback rather
      // than a one-shot decision made at the start.
      if (wsStateRef.current !== 'open') {
        const { body } = await authorizedFetchJson<LinkTest>(`/tests/${testId}`);
        if (!mountedRef.current) return;
        if (pendingTestIdRef.current !== testId) return;
        if (isTestFinished(body)) {
          pendingTestIdRef.current = null;
          setResult(body);
          setStatus('done');
          return;
        }
      }
      await sleep(POLL_INTERVAL_MS);
    }
    if (!mountedRef.current || pendingTestIdRef.current !== testId) return;
    pendingTestIdRef.current = null;
    setStatus('error');
    setError('Timed out waiting for the test result.');
  };

  const busy = status === 'starting' || status === 'polling';

  return (
    <Box sx={{ mt: 1 }}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
        <Button variant="outlined" onClick={runTest} disabled={busy}>
          {busy && <CircularProgress size={16} sx={{ mr: 1 }} />}
          {status === 'starting' && 'Starting test…'}
          {status === 'polling' && 'Waiting for result…'}
          {!busy && 'Test connection'}
        </Button>
        {status === 'polling' && (
          <Chip
            label={wsState === 'open' ? 'Live (WebSocket)' : 'Polling'}
            color={wsState === 'open' ? 'success' : 'default'}
            size="small"
          />
        )}
      </Stack>
      {status === 'done' && result && (
        <Alert severity={result.outcome === 'success' ? 'success' : 'warning'} sx={{ mt: 1 }}>
          {describeTestOutcome(result)}
        </Alert>
      )}
      {status === 'error' && error && (
        <Alert severity="error" sx={{ mt: 1 }}>
          {error}
        </Alert>
      )}
    </Box>
  );
}
