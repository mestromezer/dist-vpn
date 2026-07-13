import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AdminContext } from 'react-admin';
import { TopologyPage } from './TopologyPage';

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('TopologyPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('renders nodes and cascade edges from /nodes and /cascades as a real graph', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.includes('/nodes')) {
        return jsonResponse({
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
        });
      }
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
  });
});
