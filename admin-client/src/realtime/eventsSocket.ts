import type { Cascade, HealthEvent, LinkTest } from '../contract/types';
import { API_URL } from '../auth/httpClient';
import { getAccessToken } from '../auth/tokenStore';

/**
 * Client for Platform's `GET /events` WebSocket channel (TZ-6 §2/§4,
 * TZ-5 §4.3/§7, contracts/rest/openapi.yaml's trailing "WebSocket-канал"
 * comment). Framework-agnostic (no React) so the reconnect/backoff/fallback
 * state machine — the part most worth testing precisely, per this slice's
 * brief — has a plain unit test (eventsSocket.test.ts) independent of
 * React's render cycle. `useEventsSocket.ts` is the thin React adapter.
 *
 * ## Message envelope: cross-checked against Platform's actual source
 *
 * **Correction (this slice, resuming after an interrupted prior session)**:
 * an earlier draft of this file/doc comment claimed Platform's `/events` was
 * a stub that "sends one message `{"type":"not_implemented",...}` and
 * immediately closes," confirmed against a "locally-run Platform." That
 * claim does not match reality and has been removed rather than carried
 * forward: the sibling `platform/api-skeleton` worktree
 * (`platform/src/dist_vpn_platform/api/routes/events.py` +
 * `services/event_bus.py`) has never contained any such stub in its git
 * history (one commit added the route, already as a real in-process pub/sub
 * subscriber) — see that worktree directly if this needs re-confirming.
 * Corrected here by reading that source directly, which is more reliable
 * than an unresolvable "trust me, I ran it" claim in a doc comment anyway.
 *
 * What Platform's `/events` actually does, per `services/event_bus.py`'s own
 * doc comment: every connected subscriber receives, as JSON, every
 * `HealthEvent` row created (node heartbeat transitions, node decommission,
 * cascade creation) and every `LinkTest` that resolves. The envelope is
 * `{"type": "health_event"|"link_test", "data": {...}}` — **snake_case type
 * tags**, not the `"HealthEvent"`/`"LinkTest"` PascalCase this client
 * originally guessed from the contract's prose ("JSON wrapping one of:
 * HealthEvent, Cascade, LinkTest" — the contract never actually pins down
 * the tag's casing). `parseRealtimeMessage`/`fromTaggedShape` below now
 * accept both spellings so this keeps working regardless of which the
 * contract eventually formalizes. There is, as of this slice, no `"cascade"`
 * / `"Cascade"` message at all — `event_bus.py` explicitly flags pushing
 * `Cascade.status` directly as "not wired up yet" on Platform's side; a
 * cascade's status change is currently only observable over the socket as a
 * `health_event` whose `cascade_id` is set and `node_id` is absent (see
 * `topology/liveTopology.ts`, which now handles that shape explicitly).
 *
 * `parseRealtimeMessage`:
 *   1. Tries a discriminated envelope `{ type, data: {...} }` first, where
 *      `type` may be either Platform's actual snake_case tag
 *      (`health_event`/`link_test`) or the PascalCase form the contract
 *      prose suggests (`HealthEvent`/`Cascade`/`LinkTest`) — see
 *      `fromTaggedShape`.
 *   2. Falls back to duck-typing an untagged bare payload by the fields that
 *      distinguish the schemas from each other (see
 *      contracts/rest/openapi.yaml components.schemas) — kept as a safety
 *      net in case a future Platform revision drops the envelope, though
 *      nothing currently sends this shape.
 * Unrecognized messages are logged and dropped rather than crashing the
 * socket (see eventsSocket.test.ts).
 *
 * **What's actually been verified this slice, precisely**: the message
 * shapes above come from reading Platform's real (non-stub) source in the
 * sibling worktree, not from a live browser click-through against a running
 * Platform — no such click-through was performed this slice. See
 * admin-client/README.md's verification section for exactly what live
 * testing has and hasn't happened.
 *
 * ## Auth
 *
 * The contract offers two options for the WS handshake: "Bearer-токен как
 * query-параметр или Sec-WebSocket-Protocol". The browser WebSocket
 * constructor cannot set an Authorization header, and using
 * Sec-WebSocket-Protocol for a bearer token requires the server to echo back
 * a matching selected subprotocol (unspecified here), so this client uses
 * the query-parameter form. The query parameter must be named `token`
 * (`websocket.query_params.get("token")` in
 * `platform/src/dist_vpn_platform/api/routes/events.py`) — confirmed by
 * reading that handler directly; an earlier draft of this client used
 * `access_token`, a reasonable-sounding but wrong guess (the contract prose
 * doesn't name the parameter). Platform rejects an unrecognized/missing
 * token with a WS close (policy-violation code) before ever broadcasting
 * anything, which browsers surface to JS the same way as any other failed
 * connection (`WebSocket.onerror`/`onclose`, no HTTP status available) — so
 * `createEventsSocket` doesn't need to (and doesn't) distinguish an auth
 * rejection from any other failed attempt; both correctly count as a failed
 * attempt and back off the same way. Trade-off worth flagging for a human
 * reviewer: the access token appears in the request URL (visible in browser
 * devtools/history and any server access logs) for the lifetime of the
 * socket — acceptable here because it's the short-lived access token (not
 * the refresh token) and the contract itself sanctions this as one of two
 * supported mechanisms.
 */

export type RealtimeMessage =
  | { type: 'HealthEvent'; data: HealthEvent }
  | { type: 'Cascade'; data: Cascade }
  | { type: 'LinkTest'; data: LinkTest };

/**
 * - `connecting`: first attempt in flight, no verdict yet.
 * - `open`: a connection has proven itself (stayed open past `stableAfterMs`,
 *   or has already delivered a message) — consumers should trust WS-driven
 *   updates.
 * - `reconnecting`: lost connection (or failed to establish one) and is
 *   retrying with backoff, but hasn't yet hit `failuresBeforeUnavailable` —
 *   consumers should keep the last known state rather than react to a single
 *   blip.
 * - `unavailable`: hit the consecutive-failure threshold. Consumers should
 *   fall back to polling. The socket keeps retrying in the background with
 *   the same backoff and will report `open` again (and consumers should stop
 *   polling) if a connection later proves stable.
 */
export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'unavailable';

type Unsubscribe = () => void;

export interface EventsSocketOptions {
  /**
   * Builds the URL to connect to, invoked fresh on every connection attempt
   * (not just once) so a reconnect after the access token has been silently
   * refreshed in the background picks up the current token rather than
   * retrying forever with a stale/expired one baked into a cached URL.
   */
  getUrl: () => string;
  /** Injectable so tests never touch a real network/WebSocket global. */
  createSocket?: (url: string) => WebSocket;
  /**
   * ms. Matches the Agent control-plane's `reconnect_backoff_initial_seconds`
   * default (1.0s, see agent/src/dist_vpn_agent/config.py on the
   * agent/grpc-client-skeleton branch) — same "restart-safe, not noisy"
   * spirit for this client-side reconnect loop as TZ-5 §7 asks for
   * ("буферизация/переподключение на стороне клиента").
   */
  initialBackoffMs?: number;
  /** ms. Matches the Agent's `reconnect_backoff_max_seconds` default (30s). */
  maxBackoffMs?: number;
  /**
   * A connection must stay open this long (or deliver at least one message,
   * whichever comes first) before it's treated as "stable" and resets the
   * backoff/failure counter. Without this, a socket that opens and then
   * closes right away — which is *exactly* what Platform's `/events` is
   * documented to currently do ("closes immediately with 'not
   * implemented'") — would look like a fresh success on every single
   * attempt, backoff would never grow, and `unavailable` (and therefore the
   * polling fallback) would never trigger. This is the subtle failure mode
   * the task called out explicitly; eventsSocket.test.ts asserts it.
   */
  stableAfterMs?: number;
  /** Consecutive failed/unstable attempts before reporting 'unavailable'. */
  failuresBeforeUnavailable?: number;
}

const DEFAULT_INITIAL_BACKOFF_MS = 1000;
const DEFAULT_MAX_BACKOFF_MS = 30_000;
const DEFAULT_STABLE_AFTER_MS = 2000;
const DEFAULT_FAILURES_BEFORE_UNAVAILABLE = 3;

export interface EventsSocket {
  subscribeMessages(listener: (message: RealtimeMessage) => void): Unsubscribe;
  subscribeState(listener: (state: ConnectionState) => void): Unsubscribe;
  getState(): ConnectionState;
  /** Stops reconnect attempts and closes the underlying socket, if any. */
  close(): void;
}

export function createEventsSocket(options: EventsSocketOptions): EventsSocket {
  const {
    getUrl,
    createSocket = (url: string) => new WebSocket(url),
    initialBackoffMs = DEFAULT_INITIAL_BACKOFF_MS,
    maxBackoffMs = DEFAULT_MAX_BACKOFF_MS,
    stableAfterMs = DEFAULT_STABLE_AFTER_MS,
    failuresBeforeUnavailable = DEFAULT_FAILURES_BEFORE_UNAVAILABLE,
  } = options;

  const messageListeners = new Set<(message: RealtimeMessage) => void>();
  const stateListeners = new Set<(state: ConnectionState) => void>();

  let state: ConnectionState = 'connecting';
  let socket: WebSocket | null = null;
  let backoff = initialBackoffMs;
  let consecutiveFailures = 0;
  let stabilityTimer: ReturnType<typeof setTimeout> | undefined;
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  let stopped = false;

  function setState(next: ConnectionState) {
    if (state === next) return;
    state = next;
    stateListeners.forEach((listener) => listener(state));
  }

  function clearStabilityTimer() {
    if (stabilityTimer) {
      clearTimeout(stabilityTimer);
      stabilityTimer = undefined;
    }
  }

  /** A connection has proven itself usable — reset backoff/failure bookkeeping. */
  function markStable() {
    clearStabilityTimer();
    backoff = initialBackoffMs;
    consecutiveFailures = 0;
    setState('open');
  }

  function scheduleReconnect() {
    if (stopped) return;
    consecutiveFailures += 1;
    setState(consecutiveFailures >= failuresBeforeUnavailable ? 'unavailable' : 'reconnecting');
    const delay = backoff;
    backoff = Math.min(backoff * 2, maxBackoffMs);
    reconnectTimer = setTimeout(connect, delay);
  }

  function handleMessage(event: MessageEvent) {
    // Any inbound message is itself proof the connection works, even before
    // the stability timer fires.
    if (state !== 'open') markStable();
    const raw = typeof event.data === 'string' ? event.data : String(event.data);
    const parsed = parseRealtimeMessage(raw);
    if (!parsed) {
      console.warn('[eventsSocket] Unrecognized /events message, ignoring:', raw);
      return;
    }
    messageListeners.forEach((listener) => listener(parsed));
  }

  function connect() {
    if (stopped) return;
    let ws: WebSocket;
    try {
      ws = createSocket(getUrl());
    } catch (err) {
      console.warn('[eventsSocket] Failed to open WebSocket', err);
      scheduleReconnect();
      return;
    }
    socket = ws;
    ws.onopen = () => {
      clearStabilityTimer();
      stabilityTimer = setTimeout(markStable, stableAfterMs);
    };
    ws.onmessage = handleMessage;
    ws.onerror = () => {
      // Browsers always follow a WebSocket error with a close event, which
      // is where reconnect scheduling actually happens — handling it twice
      // (here and in onclose) would double-count the failure.
    };
    ws.onclose = () => {
      clearStabilityTimer();
      socket = null;
      scheduleReconnect();
    };
  }

  connect();

  return {
    subscribeMessages(listener) {
      messageListeners.add(listener);
      return () => {
        messageListeners.delete(listener);
      };
    },
    subscribeState(listener) {
      stateListeners.add(listener);
      return () => {
        stateListeners.delete(listener);
      };
    },
    getState() {
      return state;
    },
    close() {
      stopped = true;
      clearStabilityTimer();
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = undefined;
      }
      socket?.close();
      socket = null;
    },
  };
}

interface TaggedEnvelope {
  type: string;
  data: unknown;
}

/**
 * Recognized tags for each message kind. Platform's actual implementation
 * (sibling `platform/api-skeleton` worktree, `services/event_bus.py`) sends
 * lowercase snake_case (`health_event`, `link_test`); the PascalCase forms
 * are the contract prose's own naming for the three schemas
 * (`HealthEvent`/`Cascade`/`LinkTest`) and are kept too in case a future
 * Platform revision — or the eventual formal AsyncAPI spec the contract's
 * bottom-of-file comment promises — picks that spelling instead. There is
 * no Platform-side `cascade`/`Cascade` sender yet (see file-level doc
 * comment above) but the tag is still recognized here so this client picks
 * it up for free once Platform adds it.
 */
const HEALTH_EVENT_TAGS = new Set(['HealthEvent', 'health_event']);
const CASCADE_TAGS = new Set(['Cascade', 'cascade']);
const LINK_TEST_TAGS = new Set(['LinkTest', 'link_test']);

function fromTaggedShape(type: string, data: unknown): RealtimeMessage | null {
  if (!data || typeof data !== 'object') return null;
  if (HEALTH_EVENT_TAGS.has(type)) return { type: 'HealthEvent', data: data as HealthEvent };
  if (CASCADE_TAGS.has(type)) return { type: 'Cascade', data: data as Cascade };
  if (LINK_TEST_TAGS.has(type)) return { type: 'LinkTest', data: data as LinkTest };
  return null;
}

/**
 * Duck-typing fallback for an untagged payload, keyed off the fields that
 * actually distinguish the three schemas in openapi.yaml's
 * `components.schemas` from one another. Order matters: Cascade and
 * HealthEvent are checked before LinkTest because LinkTest's only required
 * field (`cascade_id`) is also present on HealthEvent.
 */
function fromUntaggedShape(obj: Record<string, unknown>): RealtimeMessage | null {
  if ('entry_node_id' in obj && 'exit_node_ids' in obj && 'status' in obj) {
    return { type: 'Cascade', data: obj as unknown as Cascade };
  }
  if ('from_status' in obj && 'to_status' in obj) {
    return { type: 'HealthEvent', data: obj as unknown as HealthEvent };
  }
  if ('cascade_id' in obj && ('outcome' in obj || 'initiated_by' in obj || 'latency_ms' in obj)) {
    return { type: 'LinkTest', data: obj as unknown as LinkTest };
  }
  return null;
}

export function parseRealtimeMessage(raw: string): RealtimeMessage | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!parsed || typeof parsed !== 'object') return null;

  const maybeTagged = parsed as Partial<TaggedEnvelope>;
  if (typeof maybeTagged.type === 'string' && maybeTagged.data !== undefined) {
    const tagged = fromTaggedShape(maybeTagged.type, maybeTagged.data);
    if (tagged) return tagged;
  }

  return fromUntaggedShape(parsed as Record<string, unknown>);
}

/**
 * Builds the `/events` WS URL from the same `API_URL` the REST dataProvider
 * uses, swapping the http(s) scheme for ws(s) and attaching the current
 * access token as a query parameter (see the auth trade-off note above).
 */
export function buildEventsUrl(): string {
  const url = new URL(`${API_URL}/events`, window.location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = getAccessToken();
  if (token) {
    // Param name `token` (not `access_token`) — verified against Platform's
    // actual implementation, see the file-level "Auth" doc comment above.
    url.searchParams.set('token', token);
  }
  return url.toString();
}

let sharedSocket: EventsSocket | null = null;

/**
 * Lazily creates a single shared connection the whole app reuses (so
 * TopologyPage and TestRequestButton don't each open their own /events
 * socket). Created on first use rather than eagerly at module load, since
 * the first use only ever happens after login (both consumers are behind
 * authenticated routes) — connecting before then would just retry forever
 * against an endpoint with no token to offer.
 */
export function getSharedEventsSocket(): EventsSocket {
  if (!sharedSocket) {
    sharedSocket = createEventsSocket({ getUrl: buildEventsUrl });
  }
  return sharedSocket;
}

/**
 * Closes and forgets the shared connection. Called on logout/session
 * expiry (see authProvider.ts) so a cleared/absent token doesn't leave a
 * reconnect loop spinning in the background against an endpoint it can no
 * longer authenticate to, and so the next login starts a fresh connection
 * (and fresh backoff state) rather than reusing one primed with an old
 * token in its last-attempted URL.
 */
export function closeSharedEventsSocket(): void {
  sharedSocket?.close();
  sharedSocket = null;
}
