import { Background, Controls, ReactFlow, type Node as FlowNode } from 'reactflow';
import 'reactflow/dist/style.css';
import { Card, CardContent, Typography } from '@mui/material';
import { Title } from 'react-admin';

/**
 * Placeholder route confirming `reactflow` is installed and wired into the
 * build/router. The real cascade-topology graph (nodes = VDS, edges = Hop
 * A/Hop B links, colored by Cascade.status / HealthEvent) is explicitly OUT
 * OF SCOPE for this slice — see TZ-6 §3.1 and README.md "Scope of this
 * slice" — because Platform's /cascades/{id} and /cascades/{id}/events
 * endpoints (and the /events WebSocket) aren't implemented yet either.
 */
const placeholderNodes: FlowNode[] = [
  {
    id: 'placeholder',
    position: { x: 0, y: 0 },
    data: {
      label: 'Topology graph — follow-up slice, blocked on Platform cascade endpoints',
    },
  },
];

export function TopologyPage() {
  return (
    <Card sx={{ m: 2 }}>
      <CardContent>
        <Title title="Topology (preview)" />
        <Typography variant="body2" sx={{ mb: 2 }}>
          Proof that <code>reactflow</code> is installed and renders. The real
          cascade topology graph (colored edges for link status, per TZ-6
          §3.1) ships in a follow-up slice once Platform's cascade/health
          endpoints exist.
        </Typography>
        <div style={{ height: 400 }}>
          <ReactFlow nodes={placeholderNodes} edges={[]} fitView>
            <Background />
            <Controls />
          </ReactFlow>
        </div>
      </CardContent>
    </Card>
  );
}
