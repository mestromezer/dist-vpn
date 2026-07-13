import { useEffect, useRef, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Stack } from '@mui/material';
import { useRecordContext } from 'react-admin';
import type { Cascade, LinkTest } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';
import { describeTestOutcome, isTestFinished } from './testOutcome';

const POLL_INTERVAL_MS = 1500;
const POLL_TIMEOUT_MS = 30_000;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type Status = 'idle' | 'starting' | 'polling' | 'done' | 'error';

/**
 * "Test connection" button for a Cascade (TZ-6 §3.2): POST /cascades/{id}/test,
 * then polls GET /tests/{test_id} until it resolves, rendering the outcome as
 * human-readable text via describeTestOutcome (never raw JSON/enum values).
 *
 * Reads the Cascade record from context (same pattern as the status Chip
 * fields in nodes.tsx/cascades.tsx) so it can be dropped straight into
 * <SimpleShowLayout> without prop plumbing.
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
      setStatus('error');
      setError(err instanceof Error ? err.message : 'Failed to start the test request.');
    }
  };

  const pollUntilDone = async (testId: string) => {
    const deadline = Date.now() + POLL_TIMEOUT_MS;
    while (Date.now() < deadline) {
      const { body } = await authorizedFetchJson<LinkTest>(`/tests/${testId}`);
      if (!mountedRef.current) return;
      if (isTestFinished(body)) {
        setResult(body);
        setStatus('done');
        return;
      }
      await sleep(POLL_INTERVAL_MS);
    }
    if (!mountedRef.current) return;
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
