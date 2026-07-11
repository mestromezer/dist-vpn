import type { TokenPair } from '../contract/types';

/**
 * In-memory-only token storage.
 *
 * Deliberate choice for this slice (see admin-client/README.md "Authentication
 * & token storage" for the full writeup, required by TZ-6 §5 / TZ-5 §6.3):
 *
 *  - Both the access token AND the refresh token live only in this module's
 *    closure variables — a plain JS object, never written to localStorage,
 *    sessionStorage, or a cookie readable by JS.
 *  - This fully closes the XSS-exfiltration-of-long-lived-tokens risk that
 *    TZ-5 §6.3 explicitly calls out ("localStorage запрещено для
 *    долгоживущих токенов") without us having to guess at a scheme (e.g.
 *    httpOnly cookies) that is actually the Platform's call to make, per
 *    TZ-5 §6.3's own "решение фиксируется на этапе реализации" — and the
 *    contract's /auth/refresh takes `refresh_token` as a JSON body field
 *    (not a cookie the browser attaches automatically), which only really
 *    makes sense if the client is expected to hold and submit it directly.
 *  - The tradeoff: a full page reload (F5, closed tab, etc.) clears both
 *    tokens, so the user has to log in again — there is no "remember me
 *    across reload" in this slice. That's an accepted cost for removing an
 *    entire class of token-theft bugs; it can be revisited later (e.g. if
 *    Platform adopts an httpOnly refresh-token cookie) without touching the
 *    access-token-in-memory part of this scheme.
 */

interface StoredTokens {
  accessToken: string;
  refreshToken: string;
  /** Epoch ms after which the access token should be treated as expired. */
  expiresAt: number;
}

let tokens: StoredTokens | null = null;

export function setTokens(pair: TokenPair): void {
  tokens = {
    accessToken: pair.access_token,
    refreshToken: pair.refresh_token,
    // Shave a few seconds off so we refresh slightly before the server
    // would reject the token.
    expiresAt: Date.now() + Math.max(pair.expires_in - 10, 0) * 1000,
  };
}

export function getAccessToken(): string | null {
  return tokens?.accessToken ?? null;
}

export function getRefreshToken(): string | null {
  return tokens?.refreshToken ?? null;
}

export function isAccessTokenExpired(): boolean {
  return tokens === null || Date.now() >= tokens.expiresAt;
}

export function clearTokens(): void {
  tokens = null;
}

export function hasSession(): boolean {
  return tokens !== null;
}
