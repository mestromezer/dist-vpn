import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import App from '../App';

/**
 * End-to-end (within jsdom) check of the must_change_password flow: a login
 * response with must_change_password: true must land the admin on the
 * change-password screen instead of the normal post-login route, per TZ-5
 * §6.3 ("дефолтный сгенерированный пароль требует немедленной смены").
 */

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('must_change_password redirect', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('forces the change-password screen when the login response requires it', async () => {
    const fetchMock = vi.fn(async (url: string) => {
      if (url.toString().endsWith('/auth/login')) {
        return jsonResponse({
          access_token: 'access-1',
          refresh_token: 'refresh-1',
          expires_in: 900,
          must_change_password: true,
        });
      }
      throw new Error(`Unexpected fetch in this test: ${url}`);
    });
    vi.stubGlobal('fetch', fetchMock);

    render(<App />);

    await userEvent.type(await screen.findByLabelText(/username/i), 'admin');
    await userEvent.type(screen.getByLabelText(/^password/i), 'default-password');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    expect(
      await screen.findByText(/you must change your password/i),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/current password/i)).toBeInTheDocument();
    // Must not have landed on the normal post-login nodes list.
    expect(screen.queryByText(/agent version/i)).not.toBeInTheDocument();
  });
});
