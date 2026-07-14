import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AdminContext, RecordContextProvider } from 'react-admin';
import type { Cascade } from '../contract/types';
import { TestHistoryList } from './TestHistoryList';

const cascade: Cascade = {
  id: 'cascade-1',
  entry_node_id: 'node-entry-1',
  exit_node_ids: ['node-exit-1'],
  status: 'up',
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('TestHistoryList', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders past test requests from GET /cascades/{id}/tests as a Datagrid, with human-readable outcomes', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/cascades/cascade-1/tests')) {
        return jsonResponse({
          items: [
            {
              id: 'test-1',
              cascade_id: 'cascade-1',
              outcome: 'success',
              latency_ms: 33,
              initiated_by: 'admin',
              created_at: '2026-07-10T12:00:00Z',
            },
            {
              id: 'test-2',
              cascade_id: 'cascade-1',
              outcome: 'timeout',
              created_at: '2026-07-11T08:00:00Z',
            },
          ],
          total: 2,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <RecordContextProvider value={cascade}>
          <TestHistoryList />
        </RecordContextProvider>
      </AdminContext>,
    );

    expect(await screen.findByText('test-1')).toBeInTheDocument();
    expect(screen.getByText('test-2')).toBeInTheDocument();
    // Human-readable outcome text, not the bare enum value.
    expect(screen.getByText(/succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/timed out/i)).toBeInTheDocument();
    expect(screen.queryByText(/^success$/)).not.toBeInTheDocument();
  });

  it('shows an empty-state message when the cascade has no recorded tests', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ items: [], total: 0 }));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <RecordContextProvider value={cascade}>
          <TestHistoryList />
        </RecordContextProvider>
      </AdminContext>,
    );

    expect(await screen.findByText(/no test requests recorded yet/i)).toBeInTheDocument();
  });

  it('surfaces a warning rather than crashing when the endpoint fails (e.g. a 404 for an unknown cascade id)', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ message: 'Not Found' }, 404));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <RecordContextProvider value={cascade}>
          <TestHistoryList />
        </RecordContextProvider>
      </AdminContext>,
    );

    expect(await screen.findByRole('alert')).toBeInTheDocument();
  });

  it('fetches the next page from the server (real pagination, not a client-side slice of one big fetch)', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('page=2')) {
        return jsonResponse({
          items: [
            { id: 'test-page2', cascade_id: 'cascade-1', outcome: 'success', latency_ms: 10, created_at: '2026-07-01T00:00:00Z' },
          ],
          total: 30,
        });
      }
      if (url.includes('page=1')) {
        return jsonResponse({
          items: [
            { id: 'test-page1', cascade_id: 'cascade-1', outcome: 'success', latency_ms: 10, created_at: '2026-07-12T00:00:00Z' },
          ],
          total: 30,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <RecordContextProvider value={cascade}>
          <TestHistoryList />
        </RecordContextProvider>
      </AdminContext>,
    );

    expect(await screen.findByText('test-page1')).toBeInTheDocument();
    // 30 results at page_size=25 -> a second page exists, so <Pagination>
    // must actually render controls rather than a single implied page.
    // Label text is the raw `ra.navigation.next` translation key, not
    // "Go to next page": this test's <AdminContext> has no i18nProvider
    // configured, and react-admin's useTranslate doesn't apply a call's `_`
    // default substitution without one (same reason Datagrid's own sort
    // labels render as literal `ra.sort.sort_by` here, not English prose).
    await userEvent.click(await screen.findByLabelText('ra.navigation.next'));

    expect(await screen.findByText('test-page2')).toBeInTheDocument();
    expect(screen.queryByText('test-page1')).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/cascades/cascade-1/tests?page=2&page_size=25'),
      expect.anything(),
    );
  });
});
