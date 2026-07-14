import { HttpError } from 'react-admin';
import type { TokenPair } from '../contract/types';
import {
  clearTokens,
  getAccessToken,
  getRefreshToken,
  hasSession,
  isAccessTokenExpired,
  setTokens,
} from './tokenStore';

/** Base URL of the Platform REST API. Configurable, never a secret (TZ-6 §5). */
export const API_URL =
  import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api/v1';

// Coalesce concurrent refresh attempts into a single in-flight request.
let refreshPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;

  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${API_URL}/auth/refresh`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
        if (!response.ok) {
          clearTokens();
          return false;
        }
        const pair = (await response.json()) as TokenPair;
        setTokens(pair);
        return true;
      } catch {
        clearTokens();
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  return refreshPromise;
}

export interface FetchJsonResult<T> {
  status: number;
  body: T;
}

/**
 * fetch wrapper for every authenticated call the dataProvider and
 * authProvider make. Attaches the in-memory access token, proactively
 * refreshes it once it's past its `expires_in` window, and retries a single
 * time on a 401 by forcing a refresh (covers clock skew / early expiry).
 */
export async function authorizedFetchJson<T>(
  path: string,
  options: RequestInit = {},
  allowRetry = true,
): Promise<FetchJsonResult<T>> {
  if (hasSession() && isAccessTokenExpired()) {
    await refreshAccessToken();
  }

  const headers = new Headers(options.headers);
  if (!headers.has('Content-Type') && options.body) {
    headers.set('Content-Type', 'application/json');
  }
  const accessToken = getAccessToken();
  if (accessToken) {
    headers.set('Authorization', `Bearer ${accessToken}`);
  }

  const response = await fetch(`${API_URL}${path}`, { ...options, headers });

  if (response.status === 401 && allowRetry) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return authorizedFetchJson<T>(path, options, false);
    }
  }

  const text = await response.text();
  let body: unknown;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (response.status < 200 || response.status >= 300) {
    const message =
      (body as { message?: string } | undefined)?.message ??
      response.statusText ??
      'Request failed';
    throw new HttpError(message, response.status, body);
  }

  return { status: response.status, body: body as T };
}
