import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
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
});
