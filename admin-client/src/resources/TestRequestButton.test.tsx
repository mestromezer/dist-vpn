import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecordContextProvider } from 'react-admin';
import type { Cascade } from '../contract/types';
import { TestRequestButton } from './TestRequestButton';

const cascade: Cascade = {
  id: 'cascade-1',
  entry_node_id: 'node-entry-1',
  exit_node_ids: ['node-exit-1'],
  status: 'up',
};

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('TestRequestButton', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('starts a test, polls for the result, and renders success with latency', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/cascades/cascade-1/test') && init?.method === 'POST') {
        return jsonResponse({ test_id: 'test-1' });
      }
      if (url.endsWith('/tests/test-1')) {
        return jsonResponse({
          id: 'test-1',
          cascade_id: 'cascade-1',
          outcome: 'success',
          latency_ms: 87,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <RecordContextProvider value={cascade}>
        <TestRequestButton />
      </RecordContextProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));

    // Human-readable text, not raw JSON/enum — this is the TZ-6 §3.2 requirement.
    expect(await screen.findByText(/succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/87 ms/)).toBeInTheDocument();
    expect(screen.queryByText(/"outcome"/)).not.toBeInTheDocument();
  });

  it.each([
    ['container_start_failed', /start.*container/i],
    ['tls_handshake_failed', /TLS handshake/i],
    ['proxy_no_response', /proxy did not respond/i],
    ['timeout', /timed out/i],
  ] as const)('renders a clear sentence for %s, not the bare enum value', async (outcome, expected) => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/cascades/cascade-1/test') && init?.method === 'POST') {
        return jsonResponse({ test_id: 'test-2' });
      }
      if (url.endsWith('/tests/test-2')) {
        return jsonResponse({ id: 'test-2', cascade_id: 'cascade-1', outcome });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <RecordContextProvider value={cascade}>
        <TestRequestButton />
      </RecordContextProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
  });

  it('keeps polling while the test is still in flight, then renders the resolved outcome', async () => {
    let getCallCount = 0;
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/cascades/cascade-1/test') && init?.method === 'POST') {
        return jsonResponse({ test_id: 'test-3' });
      }
      if (url.endsWith('/tests/test-3')) {
        getCallCount += 1;
        if (getCallCount === 1) {
          // Still running: no `outcome` field yet.
          return jsonResponse({ id: 'test-3', cascade_id: 'cascade-1' });
        }
        return jsonResponse({
          id: 'test-3',
          cascade_id: 'cascade-1',
          outcome: 'success',
          latency_ms: 12,
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <RecordContextProvider value={cascade}>
        <TestRequestButton />
      </RecordContextProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));

    expect(await screen.findByText(/waiting for result/i)).toBeInTheDocument();
    await waitFor(() => expect(getCallCount).toBeGreaterThanOrEqual(2), { timeout: 5000 });
    expect(await screen.findByText(/succeeded/i, undefined, { timeout: 5000 })).toBeInTheDocument();
  });
});
