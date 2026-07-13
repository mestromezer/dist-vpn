import type { AuthProvider } from 'react-admin';
import type { ChangePasswordRequest, LoginRequest, TokenPair } from '../contract/types';
import { API_URL, authorizedFetchJson } from './httpClient';
import {
  clearMustChangePassword,
  clearTokens,
  getAccessToken,
  getRefreshToken,
  hasSession,
  setTokens,
} from './tokenStore';

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

    if (pair.must_change_password) {
      // Force-navigate to the change-password screen instead of react-admin's
      // default post-login redirect. PasswordChangeGuard (see
      // auth/PasswordChangeGuard.tsx) backs this up on every route change, in
      // case the admin has a bookmarked deep link into the app.
      return { redirectTo: '/change-password' };
    }
  },

  logout: async () => {
    // Best-effort server-side revocation of the current session's refresh
    // token; we clear the local (in-memory) session regardless of whether
    // this call succeeds, since there is nothing else client-side to clean.
    const accessToken = getAccessToken();
    const refreshToken = getRefreshToken();
    clearTokens();
    if (accessToken) {
      try {
        // Per the contract update clarifying /auth/logout (see contract/types.ts
        // contract-drift note): the request body's refresh_token is optional,
        // but sending it explicitly is now the recommended way to identify
        // which session to revoke, rather than relying on the server to infer
        // it from the (possibly near-expiry) access token alone.
        const hasRefreshToken = Boolean(refreshToken);
        await fetch(`${API_URL}/auth/logout`, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${accessToken}`,
            ...(hasRefreshToken ? { 'Content-Type': 'application/json' } : {}),
          },
          body: hasRefreshToken
            ? JSON.stringify({ refresh_token: refreshToken })
            : undefined,
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

/**
 * POST /auth/change-password — not part of react-admin's AuthProvider
 * interface (there's no built-in "change password" verb), so it's a plain
 * function called directly by ChangePasswordPage rather than wired through
 * `authProvider`. Uses authorizedFetchJson (not the bare `fetch` login/logout
 * use) because this endpoint requires the bearer token, per the contract's
 * global `security: [bearerAuth]` (it has no per-path `security: []`
 * override, unlike /auth/login and /auth/refresh).
 */
export async function changePassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  const body: ChangePasswordRequest = {
    current_password: currentPassword,
    new_password: newPassword,
  };
  await authorizedFetchJson('/auth/change-password', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  clearMustChangePassword();
}
