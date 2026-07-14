import { useEffect, useState } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import { Datagrid, DateField, FunctionField, ListContextProvider, TextField, useList, useRecordContext } from 'react-admin';
import type { Cascade, LinkTest } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';
import { describeTestOutcome } from './testOutcome';

// Generous enough that pagination controls aren't needed for this slice (see
// the "simple list" scope note below) while still bounding the request —
// this endpoint has no documented upper limit on page_size in the contract.
const HISTORY_PAGE_SIZE = 100;

/**
 * Test-request history for a cascade (TZ-6 §3.2 "таблица истории"; TZ-5
 * §4.3), via `GET /cascades/{cascade_id}/tests` — added to the contract this
 * round specifically to unblock this view (see contracts/rest/openapi.yaml,
 * commit f45e2c5, and admin-client/README.md "Замечания к контракту": the
 * previous slice's README flagged that only GET /tests/{test_id} existed,
 * with no way to list past attempts, and left it as "a contract question
 * for Platform, not a client task" — that question has since been answered
 * upstream).
 *
 * Fetches directly via `authorizedFetchJson`, the same way
 * TestRequestButton.tsx does, rather than through the shared `dataProvider`:
 * the endpoint is a nested path keyed on the parent Cascade
 * (`/cascades/{id}/tests`), not the flat/query-filterable shape
 * (`/tests?cascade_id=...`) `dataProvider.getManyReference` assumes for
 * every resource (see dataProvider.ts) — special-casing one relationship
 * inside the shared provider felt like the wrong place for it.
 * `useList` + `<ListContextProvider>` is react-admin's own documented
 * pattern for getting `<Datagrid>` rendering (same column/field components
 * as CascadeList/UserList elsewhere in this codebase) over data that arrived
 * via a one-off fetch instead of the standard resource data flow.
 *
 * Scope: a single page of up to `HISTORY_PAGE_SIZE` most-recent-first
 * results, no page-through-more-than-100 control — "a simple <Datagrid>/list
 * ... is enough" per this slice's brief. If a cascade's history commonly
 * exceeds that, add real server-side pagination (the endpoint already
 * accepts `page`/`page_size`) — see README.md TODO.
 *
 * **Verification status**: `GET /cascades/{cascade_id}/tests` is real (not
 * stubbed) on Platform's `platform/api-skeleton` branch as of this slice —
 * see `list_cascade_tests` in
 * `platform/src/dist_vpn_platform/api/routes/cascades.py` in the sibling
 * worktree, read directly for this check: it returns the same
 * `{items, total}` envelope this component expects, backed by real
 * `LinkTest` rows (an earlier draft of this comment claimed the path
 * "doesn't exist at all yet" on Platform's side — that was checked against
 * stale information and is corrected here rather than carried forward). No
 * live click-through through an actual running Admin Client + Platform in a
 * browser was performed this slice, though — this component is exercised
 * only by its own mocked-fetch tests (TestHistoryList.test.tsx) plus reading
 * Platform's source directly, not by an actual request/response round trip.
 * See README.md TODO.
 */
export function TestHistoryList() {
  const record = useRecordContext<Cascade>();
  const [tests, setTests] = useState<LinkTest[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!record) return undefined;
    let cancelled = false;
    setTests(null);
    setError(null);
    authorizedFetchJson<{ items?: LinkTest[]; total?: number }>(
      `/cascades/${record.id}/tests?page=1&page_size=${HISTORY_PAGE_SIZE}`,
    )
      .then(({ body }) => {
        if (cancelled) return;
        setTests(body.items ?? []);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load test history.');
      });
    return () => {
      cancelled = true;
    };
    // Deliberately keyed on record?.id, not the whole `record` object:
    // react-admin's Show controller can hand back a new record object
    // reference on re-renders that don't change which cascade is being
    // shown, and re-fetching the whole history list on every such re-render
    // would be wasteful/flickery. Re-fetch only when we're actually looking
    // at a different cascade.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record?.id]);

  const listContext = useList<LinkTest>({
    data: tests ?? [],
    isLoading: tests === null && !error,
    perPage: HISTORY_PAGE_SIZE,
  });

  if (!record) return null;

  return (
    <Box component="section" sx={{ mt: 3 }}>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>
        Test-request history
      </Typography>
      {error && (
        <Alert severity="warning" sx={{ mb: 1 }}>
          {error}
        </Alert>
      )}
      {!error && tests !== null && tests.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          No test requests recorded yet for this cascade.
        </Typography>
      )}
      {!error && tests !== null && tests.length > 0 && (
        <ListContextProvider value={listContext}>
          {/* `resource="tests"` is required here (not registered as a
              `<Resource>` in App.tsx — this is a nested view, not a
              standalone list route): Datagrid's per-row canAccess check
              (`useCanAccess`, react-admin 5.x) throws without a resource
              name available from either a `<Resource>` ancestor or this
              prop. */}
          <Datagrid resource="tests" bulkActionButtons={false} rowClick={false}>
            <TextField source="id" label="Test ID" />
            <DateField source="created_at" showTime label="Requested at" />
            <TextField source="initiated_by" label="Initiated by" emptyText="—" />
            <FunctionField<LinkTest> label="Result" render={(test) => describeTestOutcome(test)} />
          </Datagrid>
        </ListContextProvider>
      )}
    </Box>
  );
}
