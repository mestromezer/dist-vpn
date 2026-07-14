import { describe, expect, it } from 'vitest';
import type { Cascade, Node } from '../contract/types';
import { buildTopologyGraph } from './graph';

const entryNode: Node = {
  id: 'node-entry-1',
  label: 'fra-entry-1',
  role: 'entry',
  region: 'eu-central',
  status: 'healthy',
  agent_version: '1.2.3',
  last_heartbeat_at: '2026-07-12T09:00:00Z',
};

const exitNodeHealthy: Node = {
  ...entryNode,
  id: 'node-exit-1',
  label: 'ams-exit-1',
  role: 'exit',
  status: 'healthy',
};

const exitNodeDown: Node = {
  ...entryNode,
  id: 'node-exit-2',
  label: 'ams-exit-2',
  role: 'exit',
  status: 'down',
};

describe('buildTopologyGraph', () => {
  it('lays out nodes in two columns by role and colors them by Node.status', () => {
    const { nodes } = buildTopologyGraph([entryNode, exitNodeHealthy, exitNodeDown], []);

    expect(nodes).toHaveLength(3);
    const entry = nodes.find((n) => n.id === 'node-entry-1');
    const exit1 = nodes.find((n) => n.id === 'node-exit-1');
    const exit2 = nodes.find((n) => n.id === 'node-exit-2');

    // Entry column is to the left of the exit column.
    expect(entry?.position.x).toBeLessThan(exit1!.position.x);
    expect(exit1?.position.x).toBe(exit2?.position.x);

    // Node border color reflects status — healthy is green, down is red —
    // so the failure point is visible on the node itself, not just the edge.
    expect(String((entry?.style as unknown as Record<string, string>)?.border)).toContain('#2e7d32');
    expect(String((exit2?.style as unknown as Record<string, string>)?.border)).toContain('#d32f2f');
  });

  it('creates one edge per (entry, exit) pair and styles it by Cascade.status', () => {
    const cascade: Cascade = {
      id: 'cascade-1',
      entry_node_id: 'node-entry-1',
      exit_node_ids: ['node-exit-1', 'node-exit-2'],
      status: 'down',
    };

    const { edges } = buildTopologyGraph([entryNode, exitNodeHealthy, exitNodeDown], [cascade]);

    expect(edges).toHaveLength(2);
    const edgeToDownNode = edges.find((e) => e.target === 'node-exit-2');
    expect(edgeToDownNode?.source).toBe('node-entry-1');
    // "down" cascades render in red, animated, with a label that spells out
    // that this is where the connection breaks — not a bare enum value.
    expect(String((edgeToDownNode?.style as unknown as Record<string, string>)?.stroke)).toContain('#d32f2f');
    expect(edgeToDownNode?.animated).toBe(true);
    expect(edgeToDownNode?.label).toMatch(/breaks here/i);
  });

  it('renders a healthy cascade as a solid, non-animated, non-alarming edge', () => {
    const cascade: Cascade = {
      id: 'cascade-2',
      entry_node_id: 'node-entry-1',
      exit_node_ids: ['node-exit-1'],
      status: 'up',
    };

    const { edges } = buildTopologyGraph([entryNode, exitNodeHealthy], [cascade]);

    expect(edges).toHaveLength(1);
    expect(edges[0].animated).toBe(false);
    expect(edges[0].label).toBe('OK');
    expect((edges[0].style as unknown as Record<string, string>)?.strokeDasharray).toBeUndefined();
  });
});
