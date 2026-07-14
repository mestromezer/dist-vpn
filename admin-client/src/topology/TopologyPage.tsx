import { useMemo } from 'react';
import { Background, Controls, ReactFlow } from 'reactflow';
import 'reactflow/dist/style.css';
import { Alert, Box, Card, CardContent, Chip, Typography } from '@mui/material';
import { Title } from 'react-admin';
import { buildTopologyGraph } from './graph';
import { useLiveTopologySnapshot } from './liveTopology';

/**
 * Real cascade topology graph (TZ-6 §3.1), replacing the earlier placeholder.
 * Entry nodes (VDS acting as the client-facing hop) are laid out in the left
 * column, exit-candidate nodes in the right column; one edge per
 * (Cascade.entry_node_id, exit_node_id) pair. Node border color and edge
 * color/dash-pattern/label both encode status so "где обрывается
 * соединение" (TZ-6 §3.1) is visible at a glance, not just on hover.
 *
 * Data source: `useLiveTopologySnapshot` (topology/liveTopology.ts) — a live
 * `WS /events` push once that connection proves stable, with the previous
 * slice's GET /nodes + GET /cascades polling kept as the always-on initial
 * load and the automatic fallback if the socket isn't available or drops.
 * See that file and README.md for exactly what's been cross-checked against
 * Platform's real (non-stub) `/events` source vs. still unconfirmed by an
 * actual live click-through in a browser.
 */
export function TopologyPage() {
  const { data, error, loading, source } = useLiveTopologySnapshot();

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
        <Typography
          variant="caption"
          color="text.secondary"
          sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 2 }}
        >
          {source === 'websocket' ? (
            <>
              <Chip label="Live (WebSocket)" color="success" size="small" />
              Updating in real time over WS /events.
            </>
          ) : (
            <>
              <Chip label="Polling" color="default" size="small" />
              Refreshed every 5s via GET /nodes + GET /cascades — WS /events
              is unavailable, connecting, or not yet implemented server-side
              (see README.md).
            </>
          )}
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
