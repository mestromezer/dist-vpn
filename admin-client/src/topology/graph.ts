import type { Edge as FlowEdge, Node as FlowNode } from 'reactflow';
import { MarkerType } from 'reactflow';
import type { Cascade, CascadeStatus, Node, NodeStatus } from '../contract/types';

/**
 * Pure data transform: (Node[], Cascade[]) -> ReactFlow nodes/edges. Kept
 * free of React/ReactFlow-runtime concerns (no rendering, no DOM) so it has
 * a plain unit test (graph.test.ts) independent of whether ReactFlow itself
 * can render in jsdom.
 *
 * Layout: nodes are laid out in two columns by Node.role (entry left, exit
 * right) — the contract already tells us the role, so no layout library
 * (dagre/elk) is needed for this slice. Edges are one per
 * (Cascade.entry_node_id, exit_node_id) pair, since that's the
 * entry->exit-candidate relationship TZ-6 §3.1 asks the graph to show.
 *
 * Colors/styles map directly to TZ-6 §3.1's explicit requirement ("где
 * обрывается соединение... понятным человеку"): both node border color and
 * edge color/dash-pattern/label encode status, not just a single hover-only
 * cue, so the failure point is visible without interaction.
 */

const COLUMN_X: Record<Node['role'], number> = { entry: 0, exit: 480 };
const ROW_HEIGHT = 90;

export const NODE_STATUS_COLOR: Record<NodeStatus, string> = {
  healthy: '#2e7d32',
  degraded: '#ed6c02',
  down: '#d32f2f',
  decommissioned: '#9e9e9e',
  enrolling: '#0288d1',
};

export const CASCADE_STATUS_STYLE: Record<
  CascadeStatus,
  { color: string; dashArray?: string; label: string; animated: boolean }
> = {
  up: { color: '#2e7d32', label: 'OK', animated: false },
  degraded: { color: '#ed6c02', dashArray: '6 3', label: 'Degraded', animated: false },
  down: { color: '#d32f2f', dashArray: '2 4', label: 'Down — connection breaks here', animated: true },
};

export interface TopologyGraph {
  nodes: FlowNode[];
  edges: FlowEdge[];
}

export function buildTopologyGraph(nodesList: Node[], cascades: Cascade[]): TopologyGraph {
  const byRole: Record<Node['role'], Node[]> = { entry: [], exit: [] };
  for (const node of nodesList) {
    byRole[node.role].push(node);
  }

  const position = new Map<string, { x: number; y: number }>();
  (Object.keys(byRole) as Node['role'][]).forEach((role) => {
    byRole[role].forEach((node, index) => {
      position.set(node.id, { x: COLUMN_X[role], y: index * ROW_HEIGHT });
    });
  });

  const flowNodes: FlowNode[] = nodesList.map((node) => ({
    id: node.id,
    position: position.get(node.id) ?? { x: 0, y: 0 },
    data: { label: `${node.label} — ${node.status}` },
    style: {
      border: `2px solid ${NODE_STATUS_COLOR[node.status]}`,
      borderRadius: 6,
      padding: 6,
      fontSize: 12,
      background: '#fff',
      color: '#212121',
    },
  }));

  const flowEdges: FlowEdge[] = [];
  for (const cascade of cascades) {
    const style = CASCADE_STATUS_STYLE[cascade.status];
    for (const exitId of cascade.exit_node_ids) {
      flowEdges.push({
        id: `${cascade.id}-${cascade.entry_node_id}-${exitId}`,
        source: cascade.entry_node_id,
        target: exitId,
        label: style.label,
        animated: style.animated,
        markerEnd: { type: MarkerType.ArrowClosed, color: style.color },
        style: {
          stroke: style.color,
          strokeWidth: 2,
          strokeDasharray: style.dashArray,
        },
        labelStyle: { fill: style.color, fontWeight: 600, fontSize: 11 },
        labelBgStyle: { fill: '#fff', fillOpacity: 0.85 },
        data: { cascadeId: cascade.id, status: cascade.status },
      });
    }
  }

  return { nodes: flowNodes, edges: flowEdges };
}
