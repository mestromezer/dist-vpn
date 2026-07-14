import { afterEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import { AdminContext } from 'react-admin';
import { closeSharedEventsSocket } from '../realtime/eventsSocket';
import { TopologyPage } from './TopologyPage';

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * Fully scripted fake WebSocket, same shape as realtime/eventsSocket.test.ts's
 * own fake -- stubbed as the global `WebSocket` constructor here (rather than
 * injected via `createSocket`, which eventsSocket.ts's shared-singleton
 * accessor doesn't expose a hook for) so TopologyPage's real
 * `getSharedEventsSocket()` call never attempts a real network connection in
 * jsdom, which as of jsdom 29 has its own WebSocket implementation that
 * would otherwise try to actually connect.
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

const nodesResponse = {
  items: [
    {
      id: 'node-entry-1',
      label: 'fra-entry-1',
      role: 'entry',
      region: 'eu-central',
      status: 'healthy',
      agent_version: '1.0.0',
      last_heartbeat_at: '2026-07-12T09:00:00Z',
    },
    {
      id: 'node-exit-1',
      label: 'ams-exit-1',
      role: 'exit',
      region: 'eu-west',
      status: 'down',
      agent_version: '1.0.0',
      last_heartbeat_at: '2026-07-12T09:00:00Z',
    },
  ],
  total: 2,
};

const cascadesResponseUp = {
  items: [
    {
      id: 'cascade-1',
      entry_node_id: 'node-entry-1',
      exit_node_ids: ['node-exit-1'],
      status: 'up',
    },
  ],
};

describe('TopologyPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    closeSharedEventsSocket();
    FakeWebSocket.instances = [];
  });

  it('renders nodes and cascade edges from /nodes and /cascades as a real graph', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/nodes')) return jsonResponse(nodesResponse);
      if (url.includes('/cascades')) {
        return jsonResponse({
          items: [
            {
              id: 'cascade-1',
              entry_node_id: 'node-entry-1',
              exit_node_ids: ['node-exit-1'],
              status: 'down',
            },
          ],
        });
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    // Never opens -- confirms the pre-existing REST-polling render path still
    // works unchanged with the WS layer wired in but never proving stable.
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <AdminContext>
        <TopologyPage />
      </AdminContext>,
    );

    // Node labels render with their status inline (human-readable, not a
    // separate raw-JSON blob) — this is the "real graph" replacing the
    // earlier reactflow-is-installed placeholder.
    expect(await screen.findByText(/fra-entry-1 — healthy/)).toBeInTheDocument();
    expect(await screen.findByText(/ams-exit-1 — down/)).toBeInTheDocument();

    // The edge for the down cascade carries the "breaks here" label, per
    // TZ-6 §3.1's requirement to make the failure point obvious.
    expect(await screen.findByText(/breaks here/i)).toBeInTheDocument();

    // WS never opened, so the caption should say so -- source stays 'polling'.
    expect(await screen.findByText(/^Polling$/)).toBeInTheDocument();
  });

  it('applies a live Cascade update pushed over WS /events once the socket is open, without an extra REST poll', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/nodes')) return jsonResponse(nodesResponse);
      if (url.includes('/cascades')) return jsonResponse(cascadesResponseUp);
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <AdminContext>
        <TopologyPage />
      </AdminContext>,
    );

    // Initial REST-driven render: cascade is "up", no "breaks here" label.
    await screen.findByText(/fra-entry-1 — healthy/);
    expect(screen.queryByText(/breaks here/i)).not.toBeInTheDocument();
    const callsBeforePush = fetchMock.mock.calls.length;
    expect(callsBeforePush).toBe(2); // one GET /nodes, one GET /cascades

    await act(async () => {
      lastSocket().triggerOpen();
      // A message alone is treated as proof of a working connection (see
      // eventsSocket.ts's handleMessage), so this doesn't need to wait out
      // stableAfterMs first.
      lastSocket().triggerMessage({
        type: 'Cascade',
        data: { id: 'cascade-1', entry_node_id: 'node-entry-1', exit_node_ids: ['node-exit-1'], status: 'down' },
      });
    });

    expect(await screen.findByText(/breaks here/i)).toBeInTheDocument();
    expect(await screen.findByText(/Live \(WebSocket\)/i)).toBeInTheDocument();
    // The update came from the WS push, not a second REST poll.
    expect(fetchMock.mock.calls.length).toBe(callsBeforePush);
  });

  it('falls back to resumed polling if the WS connection drops after being open', async () => {
    let cascadesCallCount = 0;
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/nodes')) return jsonResponse(nodesResponse);
      if (url.includes('/cascades')) {
        cascadesCallCount += 1;
        return jsonResponse(cascadesResponseUp);
      }
      throw new Error(`Unexpected fetch: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('WebSocket', FakeWebSocket);

    render(
      <AdminContext>
        <TopologyPage />
      </AdminContext>,
    );

    await screen.findByText(/fra-entry-1 — healthy/);
    expect(cascadesCallCount).toBe(1);

    await act(async () => {
      lastSocket().triggerOpen();
      lastSocket().triggerMessage({
        type: 'Cascade',
        data: { id: 'cascade-1', entry_node_id: 'node-entry-1', exit_node_ids: ['node-exit-1'], status: 'up' },
      });
    });
    expect(await screen.findByText(/Live \(WebSocket\)/i)).toBeInTheDocument();
    // Polling paused while WS is open -- still just the one initial fetch.
    expect(cascadesCallCount).toBe(1);

    await act(async () => {
      lastSocket().triggerClose();
    });

    // Back to 'Polling' in the UI, and usePolling's paused effect resumes
    // (fires again) rather than staying silent forever.
    expect(await screen.findByText(/^Polling$/)).toBeInTheDocument();
    await waitFor(() => expect(cascadesCallCount).toBeGreaterThanOrEqual(2), { timeout: 5000 });
  });
});
