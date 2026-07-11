import type { AuthProvider } from 'react-admin';
import type { LoginRequest, TokenPair } from '../contract/types';
import { API_URL } from './httpClient';
import { clearTokens, getAccessToken, hasSession, setTokens } from './tokenStore';

/**
 * authProvider wired against POST /auth/login, /auth/refresh (via
 * httpClient's transparent refresh) and /auth/logout, per TZ-6 §3.5 / §5 and
 * TZ-5 §6.3. See tokenStore.ts and README.md for the token-storage scheme.
 */
export const authProvider: AuthProvider = {
  login: async (params) => {
    const { username, password, totp_code } = params as LoginRequest;
    const response = await fetch(`${API_URL}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password, totp_code }),
    });

    if (!response.ok) {
      if (response.status === 401) {
        throw new Error('Invalid username, password, or TOTP code');
      }
      if (response.status === 429) {
        throw new Error('Too many login attempts — please wait and try again');
      }
      throw new Error('Login failed');
    }

    const pair = (await response.json()) as TokenPair;
    setTokens(pair);
  },

  logout: async () => {
    // Best-effort server-side revocation of the current session's refresh
    // token; we clear the local (in-memory) session regardless of whether
    // this call succeeds, since there is nothing else client-side to clean.
    const accessToken = getAccessToken();
    clearTokens();
    if (accessToken) {
      try {
        await fetch(`${API_URL}/auth/logout`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${accessToken}` },
        });
      } catch {
        // Network failure during logout must not block the client-side logout.
      }
    }
  },

  checkAuth: async () => {
    if (!hasSession()) {
      throw new Error('Not authenticated');
    }
  },

  checkError: async (error: { status?: number }) => {
    if (error?.status === 401 || error?.status === 403) {
      clearTokens();
      throw new Error('Session expired');
    }
  },

  getIdentity: async () => ({ id: 'admin' }),

  getPermissions: async () => undefined,
};
