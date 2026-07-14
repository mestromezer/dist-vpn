import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createEventsSocket, parseRealtimeMessage } from './eventsSocket';
import type { ConnectionState } from './eventsSocket';

/**
 * Fully scripted fake WebSocket: gives each test explicit control over
 * open/close/message timing instead of depending on jsdom's own (real,
 * network-hitting) WebSocket implementation. Mirrors the level of control
 * `vi.stubGlobal('fetch', ...)` gives the rest of this codebase's tests.
 */
class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  triggerOpen() {
    this.onopen?.();
  }

  triggerMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) });
  }

  triggerClose() {
    this.closed = true;
    this.onclose?.();
  }

  close() {
    if (this.closed) return;
    this.triggerClose();
  }
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  if (!socket) throw new Error('No FakeWebSocket was constructed');
  return socket;
}

describe('createEventsSocket', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  function makeSocket(overrides: Partial<Parameters<typeof createEventsSocket>[0]> = {}) {
    return createEventsSocket({
      getUrl: () => 'wss://platform.example/api/v1/events?access_token=t1',
      createSocket: (url: string) => new FakeWebSocket(url) as unknown as WebSocket,
      initialBackoffMs: 1000,
      maxBackoffMs: 8000,
      stableAfterMs: 2000,
      failuresBeforeUnavailable: 3,
      ...overrides,
    });
  }

  it('connects immediately on creation using the injected factory and getUrl()', () => {
    makeSocket();
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(lastSocket().url).toBe('wss://platform.example/api/v1/events?access_token=t1');
  });

  it('reports "open" only after the connection stays up past stableAfterMs, not immediately on open', () => {
    const socket = makeSocket();
    expect(socket.getState()).toBe('connecting');

    lastSocket().triggerOpen();
    // Open fired, but stability window hasn't elapsed yet.
    expect(socket.getState()).toBe('connecting');

    vi.advanceTimersByTime(1999);
    expect(socket.getState()).toBe('connecting');

    vi.advanceTimersByTime(1);
    expect(socket.getState()).toBe('open');
  });

  it('treats an early message as proof of a working connection, without waiting for stableAfterMs', () => {
    const socket = makeSocket();
    lastSocket().triggerOpen();
    lastSocket().triggerMessage({ type: 'Cascade', data: { id: 'c1', entry_node_id: 'e1', exit_node_ids: ['x1'], status: 'up' } });
    expect(socket.getState()).toBe('open');
  });

  it('delivers parsed messages to subscribers', () => {
    const socket = makeSocket();
    const received: unknown[] = [];
    socket.subscribeMessages((msg) => received.push(msg));

    lastSocket().triggerOpen();
    lastSocket().triggerMessage({
      type: 'Cascade',
      data: { id: 'c1', entry_node_id: 'e1', exit_node_ids: ['x1'], status: 'down' },
    });

    expect(received).toEqual([
      { type: 'Cascade', data: { id: 'c1', entry_node_id: 'e1', exit_node_ids: ['x1'], status: 'down' } },
    ]);
  });

  it('reconnects with exponential backoff on repeated failures before ever becoming stable', () => {
    const socket = makeSocket({ initialBackoffMs: 1000, maxBackoffMs: 8000 });
    const states: ConnectionState[] = [];
    socket.subscribeState((s) => states.push(s));

    // Attempt 1 fails immediately (closes without ever opening).
    lastSocket().triggerClose();
    expect(socket.getState()).toBe('reconnecting');
    expect(FakeWebSocket.instances).toHaveLength(1);

    // Backoff #1 = 1000ms.
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Attempt 2 fails too -> backoff #2 = 2000ms.
    lastSocket().triggerClose();
    expect(socket.getState()).toBe('reconnecting');
    vi.advanceTimersByTime(1999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    // Attempt 3 fails -> this is the 3rd consecutive failure, so the socket
    // should now report 'unavailable' (failuresBeforeUnavailable: 3) so
    // consumers fall back to polling, while it keeps retrying in the
    // background with continued backoff growth.
    lastSocket().triggerClose();
    expect(socket.getState()).toBe('unavailable');
    expect(states).toContain('unavailable');

    // Backoff keeps growing but is capped at maxBackoffMs (8000ms), never
    // exceeding it even after further failures.
    vi.advanceTimersByTime(3999);
    expect(FakeWebSocket.instances).toHaveLength(3);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(4);
  });

  it('does not treat a socket that opens and immediately closes (e.g. Platform\'s current /events stub) as a success', () => {
    // This is the specific subtle failure mode called out in the task: a
    // server that upgrades the connection and then closes it right away
    // (contract note: "closes immediately with 'not implemented'") must
    // still count as a failure and back off, not reset to "everything is
    // fine" on every attempt.
    const socket = makeSocket({ failuresBeforeUnavailable: 2 });

    lastSocket().triggerOpen();
    lastSocket().triggerClose(); // closes well before stableAfterMs (2000ms) elapses
    expect(socket.getState()).toBe('reconnecting');

    vi.advanceTimersByTime(1000);
    lastSocket().triggerOpen();
    lastSocket().triggerClose();
    // Second consecutive "flash open then close" -> unavailable, exactly
    // like two outright connection failures would.
    expect(socket.getState()).toBe('unavailable');
  });

  it('resets the backoff and failure count after a stable connection, and recovers to "open" after a later drop', () => {
    const socket = makeSocket({ initialBackoffMs: 1000, maxBackoffMs: 8000, failuresBeforeUnavailable: 2 });

    // First attempt fails once.
    lastSocket().triggerClose();
    expect(socket.getState()).toBe('reconnecting');
    vi.advanceTimersByTime(1000);
    expect(FakeWebSocket.instances).toHaveLength(2);

    // Second attempt succeeds and stays up past stableAfterMs.
    lastSocket().triggerOpen();
    vi.advanceTimersByTime(2000);
    expect(socket.getState()).toBe('open');

    // Now it drops. Backoff should have been reset to initialBackoffMs by
    // the stable period, not resume from wherever it left off.
    lastSocket().triggerClose();
    expect(socket.getState()).toBe('reconnecting'); // 1st failure post-stability, not yet 'unavailable' (threshold 2)
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);

    // And once it reconnects and stabilizes again, state goes back to
    // 'open' -- consumers relying on this to know when to stop polling.
    lastSocket().triggerOpen();
    vi.advanceTimersByTime(2000);
    expect(socket.getState()).toBe('open');
  });

  it('recovers from "unavailable" back to "open" once a later attempt stabilizes', () => {
    const socket = makeSocket({ failuresBeforeUnavailable: 1 });

    lastSocket().triggerClose();
    expect(socket.getState()).toBe('unavailable');

    vi.advanceTimersByTime(1000);
    lastSocket().triggerOpen();
    vi.advanceTimersByTime(2000);
    expect(socket.getState()).toBe('open');
  });

  it('stops reconnecting once closed, and construction failures are treated as a connection failure', () => {
    const factory = vi.fn(() => new FakeWebSocket('ws://x') as unknown as WebSocket);
    const socket = makeSocket({ createSocket: factory });
    expect(factory).toHaveBeenCalledTimes(1);

    socket.close();
    lastSocket().triggerClose(); // late event from the now-abandoned socket
    vi.advanceTimersByTime(60_000);
    // No further connection attempts after close(), regardless of elapsed time.
    expect(factory).toHaveBeenCalledTimes(1);
  });

  it('treats a synchronous throw from createSocket as a failed attempt and still schedules a retry', () => {
    let calls = 0;
    const socket = makeSocket({
      createSocket: () => {
        calls += 1;
        if (calls === 1) throw new Error('boom');
        return new FakeWebSocket('ws://x') as unknown as WebSocket;
      },
      initialBackoffMs: 500,
    });
    expect(socket.getState()).toBe('reconnecting');
    vi.advanceTimersByTime(500);
    expect(calls).toBe(2);
  });
});

describe('parseRealtimeMessage', () => {
  it('parses the preferred tagged envelope shape', () => {
    const msg = parseRealtimeMessage(
      JSON.stringify({ type: 'LinkTest', data: { id: 't1', cascade_id: 'c1', outcome: 'success', latency_ms: 20 } }),
    );
    expect(msg).toEqual({ type: 'LinkTest', data: { id: 't1', cascade_id: 'c1', outcome: 'success', latency_ms: 20 } });
  });

  it('parses Platform\'s actual snake_case tags (health_event, link_test) -- see services/event_bus.py in the sibling platform worktree', () => {
    const healthEvent = parseRealtimeMessage(
      JSON.stringify({
        type: 'health_event',
        data: {
          id: 'h1',
          cascade_id: null,
          node_id: 'n1',
          from_status: 'enrolling',
          to_status: 'healthy',
          reason: 'heartbeat',
          occurred_at: '2026-07-13T00:00:00Z',
        },
      }),
    );
    expect(healthEvent?.type).toBe('HealthEvent');

    const linkTest = parseRealtimeMessage(
      JSON.stringify({ type: 'link_test', data: { id: 't1', cascade_id: 'c1', outcome: 'success', latency_ms: 55 } }),
    );
    expect(linkTest?.type).toBe('LinkTest');
  });

  it('falls back to duck-typing an untagged Cascade payload', () => {
    const msg = parseRealtimeMessage(
      JSON.stringify({ id: 'c1', entry_node_id: 'e1', exit_node_ids: ['x1'], status: 'degraded' }),
    );
    expect(msg?.type).toBe('Cascade');
  });

  it('falls back to duck-typing an untagged HealthEvent payload', () => {
    const msg = parseRealtimeMessage(
      JSON.stringify({
        id: 'h1',
        cascade_id: 'c1',
        node_id: 'n1',
        from_status: 'healthy',
        to_status: 'down',
        reason: 'heartbeat timeout',
        occurred_at: '2026-07-13T00:00:00Z',
      }),
    );
    expect(msg?.type).toBe('HealthEvent');
  });

  it('falls back to duck-typing an untagged LinkTest payload', () => {
    const msg = parseRealtimeMessage(JSON.stringify({ id: 't1', cascade_id: 'c1', outcome: 'timeout' }));
    expect(msg?.type).toBe('LinkTest');
  });

  it('returns null for unparseable or unrecognized payloads instead of throwing', () => {
    expect(parseRealtimeMessage('not json')).toBeNull();
    expect(parseRealtimeMessage(JSON.stringify({ foo: 'bar' }))).toBeNull();
    expect(parseRealtimeMessage(JSON.stringify(null))).toBeNull();
  });
});
