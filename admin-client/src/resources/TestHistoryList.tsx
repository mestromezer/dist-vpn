import { useEffect, useState } from 'react';
import { Alert, Box, Typography } from '@mui/material';
import {
  Datagrid,
  DateField,
  FunctionField,
  ListContextProvider,
  Pagination,
  TextField,
  useRecordContext,
} from 'react-admin';
import type { ListControllerResult } from 'react-admin';
import type { Cascade, LinkTest } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';
import { describeTestOutcome } from './testOutcome';

// Matches GET /cascades/{id}/tests's own contract default (openapi.yaml).
const PAGE_SIZE = 25;

// The endpoint always orders most-recent-first server-side (see
// list_cascade_tests in platform/api/routes/cascades.py) and takes no sort
// param - this is a fixed label for <Datagrid>'s column, not a real toggle.
const FIXED_SORT = { field: 'created_at', order: 'DESC' as const };

const noop = () => {};

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
 * `<ListContextProvider>` gets `<Datagrid>`/`<Pagination>` rendering (same
 * column/field components as CascadeList/UserList elsewhere in this
 * codebase) over data that arrived via a one-off fetch instead of the
 * standard resource data flow. The context value is built by hand
 * (`buildListContext` below) rather than via react-admin's `useList`:
 * `useList` only paginates an already-fully-loaded local array (its `total`
 * is always `data.length`, see its source in `ra-core`), which can't
 * represent "the server has 200 more rows we haven't fetched yet" — exactly
 * the case here, since each page change re-fetches just that page from
 * `GET /cascades/{id}/tests?page=N&page_size=25` rather than loading
 * everything up front.
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
/**
 * `ListContextProvider`'s `value` prop is typed as the discriminated union
 * `ListControllerResult` (loading / error / success — see `useListController`
 * in `ra-core`), matching what `useListController`/`useList` return. Neither
 * fits a hand-rolled server-paginated fetch, so this builds the same shape
 * directly rather than pulling in either.
 */
function buildListContext(params: {
  data: LinkTest[];
  total: number;
  page: number;
  perPage: number;
  setPage: (page: number) => void;
  isPending: boolean;
  error: Error | null;
}): ListControllerResult<LinkTest> {
  const { data, total, page, perPage, setPage, isPending, error } = params;
  const base = {
    sort: FIXED_SORT,
    displayedFilters: {},
    filterValues: {},
    hideFilter: noop,
    showFilter: noop,
    setFilters: noop,
    setSort: noop,
    onSelect: noop,
    onSelectAll: noop,
    onToggleItem: noop,
    onUnselectItems: noop,
    page,
    perPage,
    refetch: noop,
    resource: 'tests',
    selectedIds: [],
    setPage,
    setPerPage: noop,
    meta: undefined,
  };
  if (isPending) {
    return { ...base, data: undefined, total: undefined, error: null, isPending: true };
  }
  if (error) {
    return { ...base, data, total, error, isPending: false };
  }
  return { ...base, data, total, error: null, isPending: false };
}

export function TestHistoryList() {
  const record = useRecordContext<Cascade>();
  const [page, setPage] = useState(1);
  const [tests, setTests] = useState<LinkTest[] | null>(null);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // A different cascade means a different history entirely - start from
  // page 1 rather than showing whatever page happened to be open before,
  // which the new cascade may not even have.
  useEffect(() => {
    setPage(1);
  }, [record?.id]);

  useEffect(() => {
    if (!record) return undefined;
    let cancelled = false;
    setTests(null);
    setError(null);
    authorizedFetchJson<{ items?: LinkTest[]; total?: number }>(
      `/cascades/${record.id}/tests?page=${page}&page_size=${PAGE_SIZE}`,
    )
      .then(({ body }) => {
        if (cancelled) return;
        setTests(body.items ?? []);
        setTotal(body.total ?? 0);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load test history.');
      });
    return () => {
      cancelled = true;
    };
    // Deliberately keyed on record?.id (not the whole `record` object) and
    // page: react-admin's Show controller can hand back a new record object
    // reference on re-renders that don't change which cascade is being
    // shown, and re-fetching on every such re-render would be
    // wasteful/flickery. Re-fetch only when we're looking at a different
    // cascade or a different page of its history.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [record?.id, page]);

  if (!record) return null;

  const listContext = buildListContext({
    data: tests ?? [],
    total,
    page,
    perPage: PAGE_SIZE,
    setPage,
    isPending: tests === null && !error,
    error: error ? new Error(error) : null,
  });

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
      {!error && tests !== null && total === 0 && (
        <Typography variant="body2" color="text.secondary">
          No test requests recorded yet for this cascade.
        </Typography>
      )}
      {!error && tests !== null && total > 0 && (
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
          {/* Single-valued rowsPerPageOptions: page size is fixed to what
              GET /cascades/{id}/tests is fetched with (setPerPage above is a
              no-op), so MUI hides the rows-per-page selector rather than
              offering a control that silently does nothing. */}
          <Pagination rowsPerPageOptions={[PAGE_SIZE]} />
        </ListContextProvider>
      )}
    </Box>
  );
}
