import { afterEach, describe, expect, it, vi } from 'vitest';
import { dataProvider } from './dataProvider';

describe('dataProvider.getList', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('maps the {items, total} envelope from GET /nodes to react-admin shape', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              id: 'node-1',
              label: 'fra-entry-1',
              role: 'entry',
              region: 'eu-central',
              status: 'healthy',
              agent_version: '1.2.3',
              last_heartbeat_at: '2026-07-11T10:00:00Z',
            },
          ],
          total: 1,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await dataProvider.getList('nodes', {
      pagination: { page: 1, perPage: 25 },
      sort: { field: 'id', order: 'ASC' },
      filter: {},
    });

    expect(result.total).toBe(1);
    expect(result.data).toHaveLength(1);
    expect(result.data[0]).toMatchObject({ id: 'node-1', status: 'healthy' });

    const requestedUrl = fetchMock.mock.calls[0][0] as string;
    expect(requestedUrl).toContain('/nodes');
    expect(requestedUrl).toContain('page=1');
    expect(requestedUrl).toContain('page_size=25');
  });
});
