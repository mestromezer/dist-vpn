import { useMemo } from 'react';
import { Background, Controls, ReactFlow } from 'reactflow';
import 'reactflow/dist/style.css';
import { Alert, Box, Card, CardContent, Typography } from '@mui/material';
import { Title } from 'react-admin';
import { authorizedFetchJson } from '../auth/httpClient';
import type { Cascade, Node } from '../contract/types';
import { buildTopologyGraph } from './graph';
import { usePolling } from './usePolling';

const POLL_INTERVAL_MS = 5000;
// Large enough to cover the pool size expected for this slice without
// needing pagination in the graph itself; GET /cascades isn't paginated in
// the contract at all (no page/page_size params), so it always returns the
// full list.
const NODES_PAGE_SIZE = 500;

interface TopologySnapshot {
  nodes: Node[];
  cascades: Cascade[];
}

async function fetchTopologySnapshot(): Promise<TopologySnapshot> {
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
 * Real cascade topology graph (TZ-6 §3.1), replacing the earlier placeholder.
 * Entry nodes (VDS acting as the client-facing hop) are laid out in the left
 * column, exit-candidate nodes in the right column; one edge per
 * (Cascade.entry_node_id, exit_node_id) pair. Node border color and edge
 * color/dash-pattern/label both encode status so "где обрывается
 * соединение" (TZ-6 §3.1) is visible at a glance, not just on hover.
 *
 * Data comes from polling GET /nodes and GET /cascades on an interval
 * (usePolling) rather than a live WebSocket push — Platform's `/events`
 * WS channel (TZ-6 §2) isn't implemented yet. See README.md TODO.
 */
export function TopologyPage() {
  const { data, error, loading } = usePolling(fetchTopologySnapshot, POLL_INTERVAL_MS);

  const { nodes: flowNodes, edges: flowEdges } = useMemo(
    () => buildTopologyGraph(data?.nodes ?? [], data?.cascades ?? []),
    [data],
  );

  return (
    <Card sx={{ m: 2 }}>
      <CardContent>
        <Title title="Cascade topology" />
        <Typography variant="body2" sx={{ mb: 1 }}>
          Entry nodes (left column) to exit-candidate nodes (right column).
          Edge color/dash pattern shows the cascade&apos;s status: solid green
          is up, dashed orange is degraded, animated dashed red is down —
          i.e. exactly where the connection currently breaks.
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mb: 2 }}>
          Refreshed every {POLL_INTERVAL_MS / 1000}s by polling GET /nodes and
          GET /cascades (interim mechanism for this slice — live push over
          Platform&apos;s WS /events channel is still TODO, see README.md).
        </Typography>
        {error && (
          <Alert severity="warning" sx={{ mb: 2 }}>
            {error} — showing the last successful snapshot below, if any.
          </Alert>
        )}
        {loading && !data ? (
          <Typography variant="body2">Loading topology…</Typography>
        ) : (
          <Box sx={{ height: 500 }}>
            <ReactFlow nodes={flowNodes} edges={flowEdges} fitView>
              <Background />
              <Controls />
            </ReactFlow>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
