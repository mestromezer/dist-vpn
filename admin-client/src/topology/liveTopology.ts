import { useCallback, useEffect, useState } from 'react';
import type { Cascade, CascadeStatus, Node, NodeStatus } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';
import type { ConnectionState, RealtimeMessage } from '../realtime/eventsSocket';
import { useEventsSocket } from '../realtime/useEventsSocket';
import { usePolling } from './usePolling';

const POLL_INTERVAL_MS = 5000;
// Large enough to cover the pool size expected for this slice without
// needing pagination in the graph itself; GET /cascades isn't paginated in
// the contract at all (no page/page_size params), so it always returns the
// full list.
const NODES_PAGE_SIZE = 500;

export interface TopologySnapshot {
  nodes: Node[];
  cascades: Cascade[];
}

export async function fetchTopologySnapshot(): Promise<TopologySnapshot> {
  const [nodesResponse, cascadesResponse] = await Promise.all([
    authorizedFetchJson<{ items?: Node[] }>(`/nodes?page_size=${NODES_PAGE_SIZE}`),
    authorizedFetchJson<{ items?: Cascade[] }>('/cascades'),
  ]);
  return {
    nodes: nodesResponse.body.items ?? [],
    cascades: cascadesResponse.body.items ?? [],
  };
}

/**
 * Pure merge: applies one `/events` push message on top of the last known
 * topology snapshot. Kept free of React so it has a plain unit test
 * (liveTopology.test.ts) independent of the hook/rendering below it — same
 * split as graph.ts/graph.test.ts.
 *
 * - `Cascade`: upsert by id (replace the matching cascade, or append if this
 *   is a cascade the last REST snapshot didn't have yet — e.g. created by
 *   another admin session after this one loaded). Not currently sent by
 *   Platform (see eventsSocket.ts's file-level doc comment — `event_bus.py`
 *   explicitly flags this as not wired up yet), but handled here so it's
 *   picked up automatically once Platform adds it.
 * - `HealthEvent`: a node *or* cascade status transition — Platform reuses
 *   the same `HealthEvent` row/broadcast for both (see
 *   `platform/src/dist_vpn_platform/api/routes/{nodes,cascades}.py`'s
 *   `publish_health_event` call sites, read directly from the sibling
 *   worktree this slice): a heartbeat-driven or decommission transition sets
 *   `node_id` (no `cascade_id`); a cascade-assembly transition (e.g. cascade
 *   created, status computed as up/degraded/down) sets `cascade_id` and
 *   leaves `node_id` null. This function branches on which id is present:
 *     - `node_id` set -> patches the matching Node's `status`.
 *     - `node_id` absent, `cascade_id` set -> patches the matching Cascade's
 *       `status`. This is the *only* way a cascade's status currently
 *       updates live (Platform has no dedicated `Cascade` broadcast yet, see
 *       above) — this is how TZ-6 §3.1's "where the connection breaks"
 *       indicator actually stays live today, not via the `Cascade` case.
 *   Cast note: `to_status` is typed as a bare `string` in contract/types.ts
 *   for both Node/Cascade statuses (the contract doesn't constrain it to
 *   either enum), so this trusts the server to send one of the known values
 *   for whichever kind of transition it is.
 *   No matching node/cascade (e.g. a brand-new cascade this session's
 *   initial snapshot predates, which can't be reconstructed from a
 *   `HealthEvent` alone — it carries no entry/exit node ids) -> message is a
 *   no-op. Known limitation, not a silent bug: a cascade created by another
 *   admin session while this one's WS is open won't appear until either
 *   Platform adds a real `Cascade` broadcast or the socket drops and polling
 *   resumes. See README.md TODO.
 * - `LinkTest`: not part of the topology graph (no node/cascade field this
 *   view renders changes as a result of a test outcome) — ignored here.
 *   TestRequestButton.tsx is where LinkTest pushes matter.
 *
 * Returns the *same* object reference when a message doesn't change
 * anything observable (unmatched HealthEvent, LinkTest, unknown message
 * type) so callers relying on referential equality (e.g. a `useEffect`
 * dependency) don't re-run for a no-op message.
 */
export function applyRealtimeMessage(
  prev: TopologySnapshot | null,
  message: RealtimeMessage,
): TopologySnapshot | null {
  if (!prev) return prev;
  switch (message.type) {
    case 'Cascade': {
      const incoming = message.data;
      const index = prev.cascades.findIndex((c) => c.id === incoming.id);
      const cascades =
        index === -1
          ? [...prev.cascades, incoming]
          : prev.cascades.map((c, i) => (i === index ? incoming : c));
      return { ...prev, cascades };
    }
    case 'HealthEvent': {
      const { node_id: nodeId, cascade_id: cascadeId, to_status: toStatus } = message.data;
      if (nodeId) {
        const index = prev.nodes.findIndex((n) => n.id === nodeId);
        if (index === -1) return prev;
        const nodes = prev.nodes.map((n, i) =>
          i === index ? { ...n, status: toStatus as NodeStatus } : n,
        );
        return { ...prev, nodes };
      }
      if (cascadeId) {
        const index = prev.cascades.findIndex((c) => c.id === cascadeId);
        if (index === -1) return prev;
        const cascades = prev.cascades.map((c, i) =>
          i === index ? { ...c, status: toStatus as CascadeStatus } : c,
        );
        return { ...prev, cascades };
      }
      return prev;
    }
    default:
      return prev;
  }
}

export interface LiveTopologyState {
  data: TopologySnapshot | null;
  error: string | null;
  loading: boolean;
  /**
   * `'websocket'` once the `/events` connection has proven itself stable and
   * REST polling has paused in its favor; `'polling'` otherwise (initial
   * load, WS still connecting/reconnecting, or WS deemed unavailable) — see
   * TopologyPage.tsx, which surfaces this in the UI so it's obvious which
   * transport is actually driving the graph at any moment, not just assumed.
   */
  source: 'websocket' | 'polling';
}

/**
 * WS-primary, polling-fallback data source for the topology graph.
 *
 * - Always polls (`usePolling`) until the shared `/events` socket reports
 *   `'open'` — this guarantees an initial full snapshot loads over REST
 *   regardless of how fast (or whether) the socket connects, since the
 *   socket only ever pushes incremental deltas, never a full snapshot.
 * - Once the socket is `'open'`, polling pauses (`usePolling`'s `enabled`
 *   option) and further updates come from `applyRealtimeMessage` merging
 *   pushed messages onto the last snapshot.
 * - If the socket later drops (`'reconnecting'`/`'unavailable'`), polling
 *   resumes automatically and the next successful poll becomes the new
 *   baseline (superseding any deltas applied on top of the previous one).
 *
 * Code-complete and cross-checked against Platform's actual (non-stub)
 * `/events` broadcaster source in the sibling worktree (see eventsSocket.ts's
 * file-level doc comment for what that means precisely) — but the
 * apply-a-live-Cascade/HealthEvent-update path itself is exercised by
 * liveTopology.test.ts's unit tests and a WS-message-injection test in
 * TopologyPage.test.tsx, not by an actual live click-through against a
 * running Platform + running Admin Client in a browser. See README.md TODO.
 */
export function useLiveTopologySnapshot(): LiveTopologyState {
  const [snapshot, setSnapshot] = useState<TopologySnapshot | null>(null);

  const handleMessage = useCallback((message: RealtimeMessage) => {
    setSnapshot((prev) => applyRealtimeMessage(prev, message));
  }, []);

  const wsState: ConnectionState = useEventsSocket(handleMessage);
  const polling = usePolling(fetchTopologySnapshot, POLL_INTERVAL_MS, {
    enabled: wsState !== 'open',
  });

  // A fresh full snapshot from a poll always supersedes any deltas applied
  // on top of the previous one. Only reacts to `polling.data` actually
  // changing reference (a new successful fetch) — while polling is paused
  // (WS open) `polling.data` stays the same object, so this effect stays
  // quiet and doesn't clobber WS-applied deltas.
  useEffect(() => {
    if (polling.data) setSnapshot(polling.data);
  }, [polling.data]);

  return {
    data: snapshot,
    error: polling.error,
    loading: polling.loading && !snapshot,
    source: wsState === 'open' ? 'websocket' : 'polling',
  };
}
