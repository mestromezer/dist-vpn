import type { DataProvider } from 'react-admin';
import { authorizedFetchJson } from './auth/httpClient';

/**
 * Thin custom data provider wrapping fetch (via authorizedFetchJson),
 * instead of ra-data-simple-rest.
 *
 * Why not ra-data-simple-rest as-is: react-admin's simple-rest provider
 * expects list responses as a bare JSON array with the total count carried
 * in a `Content-Range` (or similar) response *header*. This contract's list
 * endpoints instead return a JSON envelope `{ items: [...], total: N }`
 * (see contracts/rest/openapi.yaml, GET /nodes) and page/page_size query
 * params rather than a Range header. That mismatch is exactly the kind of
 * "different envelope/pagination convention" the task called out — small
 * enough to not need ra-data-simple-rest as a base, so this adapter talks to
 * the contract directly instead of fighting the header-based convention.
 *
 * Scope for this slice: only the endpoints needed by the `nodes` resource
 * (list/show) are exercised end-to-end and manually verified. The other
 * methods follow the same REST conventions used elsewhere in the contract
 * (PATCH for update, POST for create, DELETE for delete) so that adding the
 * next <Resource> (cascades, users) mostly means pointing react-admin at it
 * rather than writing new plumbing - but they are untested until that
 * resource actually lands.
 */
export const dataProvider: DataProvider = {
  getList: async (resource, params) => {
    const { page = 1, perPage = 25 } = params.pagination ?? {};
    const query = buildQuery({
      ...params.filter,
      page,
      page_size: perPage,
    });
    const { body } = await authorizedFetchJson<ListEnvelope>(
      `/${resource}${query}`,
    );
    const items = body.items ?? [];
    if (body.total === undefined) {
      // Contract gap, not a bug here: /cascades and /users list responses
      // don't include a `total` field in openapi.yaml (only /nodes does),
      // so react-admin can't compute an exact page count for them yet.
      // Flagged to the contract owners rather than worked around silently.
      console.warn(
        `[dataProvider] "${resource}" list response has no "total" field; ` +
          'falling back to items.length, which undercounts total pages.',
      );
    }
    return { data: items, total: body.total ?? items.length };
  },

  getOne: async (resource, params) => {
    const { body } = await authorizedFetchJson<RaLikeRecord>(
      `/${resource}/${params.id}`,
    );
    return { data: body };
  },

  getMany: async (resource, params) => {
    // No batch-get endpoint in the contract; fetch each record individually.
    const data = await Promise.all(
      params.ids.map((id) =>
        authorizedFetchJson<RaLikeRecord>(`/${resource}/${id}`).then(
          (r) => r.body,
        ),
      ),
    );
    return { data };
  },

  getManyReference: async (resource, params) => {
    const { page = 1, perPage = 25 } = params.pagination ?? {};
    const query = buildQuery({
      ...params.filter,
      [params.target]: params.id,
      page,
      page_size: perPage,
    });
    const { body } = await authorizedFetchJson<ListEnvelope>(
      `/${resource}${query}`,
    );
    const items = body.items ?? [];
    return { data: items, total: body.total ?? items.length };
  },

  create: async (resource, params) => {
    const { body } = await authorizedFetchJson<RaLikeRecord>(`/${resource}`, {
      method: 'POST',
      body: JSON.stringify(params.data),
    });
    return { data: body };
  },

  update: async (resource, params) => {
    const { body } = await authorizedFetchJson<RaLikeRecord>(
      `/${resource}/${params.id}`,
      { method: 'PATCH', body: JSON.stringify(params.data) },
    );
    return { data: body };
  },

  updateMany: async (resource, params) => {
    await Promise.all(
      params.ids.map((id) =>
        authorizedFetchJson(`/${resource}/${id}`, {
          method: 'PATCH',
          body: JSON.stringify(params.data),
        }),
      ),
    );
    return { data: params.ids };
  },

  delete: async (resource, params) => {
    await authorizedFetchJson(`/${resource}/${params.id}`, {
      method: 'DELETE',
    });
    return { data: (params.previousData ?? { id: params.id }) as RaLikeRecord };
  },

  deleteMany: async (resource, params) => {
    await Promise.all(
      params.ids.map((id) =>
        authorizedFetchJson(`/${resource}/${id}`, { method: 'DELETE' }),
      ),
    );
    return { data: params.ids };
  },
};

interface ListEnvelope {
  items?: RaLikeRecord[];
  total?: number;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type RaLikeRecord = any;

function buildQuery(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    search.set(key, String(value));
  });
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}
