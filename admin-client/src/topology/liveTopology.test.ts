import { describe, expect, it } from 'vitest';
import type { Cascade, Node } from '../contract/types';
import type { RealtimeMessage } from '../realtime/eventsSocket';
import { applyRealtimeMessage, type TopologySnapshot } from './liveTopology';

const entryNode: Node = {
  id: 'node-entry-1',
  label: 'fra-entry-1',
  role: 'entry',
  region: 'eu-central',
  status: 'healthy',
  agent_version: '1.0.0',
  last_heartbeat_at: '2026-07-13T09:00:00Z',
};

const exitNode: Node = {
  id: 'node-exit-1',
  label: 'ams-exit-1',
  role: 'exit',
  region: 'eu-west',
  status: 'healthy',
  agent_version: '1.0.0',
  last_heartbeat_at: '2026-07-13T09:00:00Z',
};

const cascade: Cascade = {
  id: 'cascade-1',
  entry_node_id: 'node-entry-1',
  exit_node_ids: ['node-exit-1'],
  status: 'up',
};

function snapshot(): TopologySnapshot {
  return { nodes: [entryNode, exitNode], cascades: [cascade] };
}

describe('applyRealtimeMessage', () => {
  it('returns null unchanged when there is no baseline snapshot yet', () => {
    const message: RealtimeMessage = { type: 'Cascade', data: { ...cascade, status: 'down' } };
    expect(applyRealtimeMessage(null, message)).toBeNull();
  });

  it('upserts (replaces) an existing cascade by id on a Cascade message', () => {
    const prev = snapshot();
    const updated: Cascade = { ...cascade, status: 'down' };
    const next = applyRealtimeMessage(prev, { type: 'Cascade', data: updated });

    expect(next).not.toBe(prev); // new object, not mutated in place
    expect(next?.cascades).toEqual([updated]);
    expect(next?.nodes).toBe(prev.nodes); // untouched half of the snapshot is reused
  });

  it('appends a Cascade message for a cascade not present in the last REST snapshot', () => {
    const prev = snapshot();
    const brandNew: Cascade = {
      id: 'cascade-2',
      entry_node_id: 'node-entry-1',
      exit_node_ids: ['node-exit-1'],
      status: 'up',
    };
    const next = applyRealtimeMessage(prev, { type: 'Cascade', data: brandNew });
    expect(next?.cascades).toEqual([cascade, brandNew]);
  });

  it('patches the matching node\'s status on a HealthEvent message', () => {
    const prev = snapshot();
    const next = applyRealtimeMessage(prev, {
      type: 'HealthEvent',
      data: {
        id: 'evt-1',
        cascade_id: 'cascade-1',
        node_id: 'node-exit-1',
        from_status: 'healthy',
        to_status: 'down',
        reason: 'heartbeat timeout',
        occurred_at: '2026-07-13T10:00:00Z',
      },
    });

    expect(next?.nodes.find((n) => n.id === 'node-exit-1')?.status).toBe('down');
    expect(next?.nodes.find((n) => n.id === 'node-entry-1')?.status).toBe('healthy');
    expect(next?.cascades).toBe(prev.cascades); // untouched half reused
  });

  it('patches the matching cascade\'s status on a HealthEvent with cascade_id set and node_id absent -- the shape Platform actually sends on cascade creation (api/routes/cascades.py::create_cascade)', () => {
    const prev = snapshot();
    const next = applyRealtimeMessage(prev, {
      type: 'HealthEvent',
      data: {
        id: 'evt-2',
        cascade_id: 'cascade-1',
        node_id: null as unknown as string,
        from_status: 'down',
        to_status: 'up',
        reason: 'cascade created by admin alice',
        occurred_at: '2026-07-13T10:00:00Z',
      },
    });

    expect(next?.cascades.find((c) => c.id === 'cascade-1')?.status).toBe('up');
    expect(next?.nodes).toBe(prev.nodes); // untouched half reused
  });

  it('is a no-op (same reference) for a cascade-targeted HealthEvent naming a cascade not in the snapshot', () => {
    const prev = snapshot();
    const next = applyRealtimeMessage(prev, {
      type: 'HealthEvent',
      data: {
        id: 'evt-3',
        cascade_id: 'cascade-unknown',
        node_id: null as unknown as string,
        from_status: 'down',
        to_status: 'up',
        reason: 'cascade created by admin alice',
        occurred_at: '2026-07-13T10:00:00Z',
      },
    });
    expect(next).toBe(prev);
  });

  it('is a no-op (same reference) for a HealthEvent naming a node not in the snapshot', () => {
    const prev = snapshot();
    const next = applyRealtimeMessage(prev, {
      type: 'HealthEvent',
      data: {
        id: 'evt-1',
        cascade_id: 'cascade-1',
        node_id: 'node-unknown',
        from_status: 'healthy',
        to_status: 'down',
        reason: 'heartbeat timeout',
        occurred_at: '2026-07-13T10:00:00Z',
      },
    });
    expect(next).toBe(prev);
  });

  it('is a no-op (same reference) for a LinkTest message -- not part of the topology graph', () => {
    const prev = snapshot();
    const next = applyRealtimeMessage(prev, {
      type: 'LinkTest',
      data: { id: 'test-1', cascade_id: 'cascade-1', outcome: 'success', latency_ms: 20 },
    });
    expect(next).toBe(prev);
  });
});
