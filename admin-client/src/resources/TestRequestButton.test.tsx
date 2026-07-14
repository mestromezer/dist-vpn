import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { RecordContextProvider } from 'react-admin';
import type { Cascade } from '../contract/types';
import { closeSharedEventsSocket } from '../realtime/eventsSocket';
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

/**
 * Same fully scripted fake WebSocket as topology/TopologyPage.test.tsx --
 * see that file's comment for why it's stubbed globally rather than
 * injected: getSharedEventsSocket() (realtime/eventsSocket.ts) is a
 * module-level singleton with no test-only override hook, and jsdom 29 has
 * its own real WebSocket that would otherwise attempt an actual connection.
 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  triggerOpen() {
    this.onopen?.();
  }

  triggerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  triggerClose() {
    this.closed = true;
    this.onclose?.();
  }

  close() {
    if (this.closed) return;
    this.triggerClose();
  }
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  if (!socket) throw new Error('No FakeWebSocket was constructed');
  return socket;
}

describe('TestRequestButton', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    closeSharedEventsSocket();
    FakeWebSocket.instances = [];
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
    // WS never opens in this test -- exercises the pre-existing REST-only
    // fallback path unchanged.
    vi.stubGlobal('WebSocket', FakeWebSocket);

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
    vi.stubGlobal('WebSocket', FakeWebSocket);

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
    vi.stubGlobal('WebSocket', FakeWebSocket);

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

  it('resolves from a live WS /events LinkTest push, without ever calling GET /tests/{id}', async () => {
    const getTestSpy = vi.fn();
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/cascades/cascade-1/test') && init?.method === 'POST') {
        return jsonResponse({ test_id: 'test-ws-1' });
      }
      if (url.endsWith('/tests/test-ws-1')) {
        getTestSpy();
        return jsonResponse({ id: 'test-ws-1', cascade_id: 'cascade-1' });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <RecordContextProvider value={cascade}>
        <TestRequestButton />
      </RecordContextProvider>,
    );

    // Open the WS *before* starting the test, so it's already 'open' (proven
    // stable by an early message) by the time pollUntilDone starts its loop
    // and checks wsStateRef.
    await act(async () => {
      lastSocket().triggerOpen();
      lastSocket().triggerMessage({ type: 'LinkTest', data: { id: 'unrelated', cascade_id: 'cascade-1', outcome: 'success' } });
    });

    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));
    await screen.findByText(/waiting for result/i);
    // The "Live (WebSocket)" indicator only renders while a test is
    // in-flight (status === 'polling') -- checked here, not before the
    // click, since there's nothing to indicate the transport for yet
    // beforehand.
    expect(await screen.findByText(/Live \(WebSocket\)/i)).toBeInTheDocument();

    await act(async () => {
      lastSocket().triggerMessage({
        type: 'LinkTest',
        data: { id: 'test-ws-1', cascade_id: 'cascade-1', outcome: 'success', latency_ms: 42 },
      });
    });

    expect(await screen.findByText(/succeeded/i)).toBeInTheDocument();
    expect(screen.getByText(/42 ms/)).toBeInTheDocument();
    // The result came from the WS push -- REST polling was skipped entirely
    // while the connection was open.
    expect(getTestSpy).not.toHaveBeenCalled();
  });

  it('ignores a LinkTest push for a different (unrelated) test id and keeps waiting', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/cascades/cascade-1/test') && init?.method === 'POST') {
        return jsonResponse({ test_id: 'test-real' });
      }
      if (url.endsWith('/tests/test-real')) {
        return jsonResponse({ id: 'test-real', cascade_id: 'cascade-1', outcome: 'success', latency_ms: 5 });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <RecordContextProvider value={cascade}>
        <TestRequestButton />
      </RecordContextProvider>,
    );

    await act(async () => {
      lastSocket().triggerOpen();
      lastSocket().triggerMessage({ type: 'LinkTest', data: { id: 'bootstrap', cascade_id: 'cascade-1', outcome: 'success' } });
    });

    await userEvent.click(screen.getByRole('button', { name: /test connection/i }));
    await screen.findByText(/waiting for result/i);

    await act(async () => {
      lastSocket().triggerMessage({
        type: 'LinkTest',
        data: { id: 'some-other-test', cascade_id: 'cascade-1', outcome: 'timeout' },
      });
    });

    // Unrelated test id -- still waiting, not resolved to the wrong outcome.
    expect(screen.getByText(/waiting for result/i)).toBeInTheDocument();
    // Falls back to REST once the socket is closed, still resolving correctly.
    await act(async () => {
      lastSocket().triggerClose();
    });
    expect(await screen.findByText(/succeeded/i, undefined, { timeout: 5000 })).toBeInTheDocument();
  });
});
