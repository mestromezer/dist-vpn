import { useEffect, useRef, useState } from 'react';
import type { ConnectionState, RealtimeMessage } from './eventsSocket';
import { getSharedEventsSocket } from './eventsSocket';

/**
 * Subscribes to the shared `/events` WebSocket (eventsSocket.ts) for the
 * lifetime of the calling component, invoking `onMessage` for every message
 * and returning the current `ConnectionState` (re-rendering the component
 * whenever it changes, e.g. `open` -> `unavailable` -> `open`).
 *
 * `onMessage` doesn't need to be memoized by the caller — like
 * topology/usePolling.ts's `fetcherRef`, it's kept in a ref that's updated on
 * every render, so the subscription (set up once, on mount) always calls the
 * latest closure instead of a stale one from the first render.
 */
export function useEventsSocket(onMessage: (message: RealtimeMessage) => void): ConnectionState {
  const socket = getSharedEventsSocket();
  const [state, setState] = useState<ConnectionState>(() => socket.getState());
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  useEffect(() => {
    // Re-sync in case the state changed between render and this effect
    // running (e.g. the socket transitioned while mounting).
    setState(socket.getState());
    const unsubscribeState = socket.subscribeState(setState);
    const unsubscribeMessages = socket.subscribeMessages((message) => handlerRef.current(message));
    return () => {
      unsubscribeState();
      unsubscribeMessages();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- socket is a stable singleton; onMessage identity is read via handlerRef, not as a dependency.
  }, [socket]);

  return state;
}
