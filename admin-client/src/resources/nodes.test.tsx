import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AdminContext } from 'react-admin';
import { NodeEnroll } from './nodes';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/**
 * NodeEnroll (TZ-6 §3.1 "форма добавления ноды в пул") is the one piece of
 * nodes.tsx with actual custom logic — POST /nodes doesn't return a Node
 * record (see the component's own doc comment), so unlike NodeEdit/NodeList
 * it can't be exercised implicitly through react-admin's own well-tested
 * Edit/Datagrid machinery. Same "test the custom-fetch logic, not the
 * library's CRUD wiring" split as TestRequestButton.test.tsx/
 * TestHistoryList.test.tsx.
 */
describe('NodeEnroll', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('POSTs the selected role/label and displays the one-time enrollment token', async () => {
    const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
      expect(url).toContain('/nodes');
      expect(init?.method).toBe('POST');
      expect(JSON.parse(String(init?.body))).toEqual({ requested_role: 'entry', label: 'ru-msk-1' });
      return jsonResponse(
        { enrollment_token: 'super-secret-token', expires_at: '2026-07-14T12:00:00Z' },
        201,
      );
    });
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <NodeEnroll />
      </AdminContext>,
    );

    await userEvent.type(screen.getByLabelText(/label/i), 'ru-msk-1');
    await userEvent.click(screen.getByRole('button', { name: /generate enrollment token/i }));

    expect(await screen.findByDisplayValue('super-secret-token')).toBeInTheDocument();
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
    // The role/label form is gone once the token is shown - nothing left to
    // resubmit until "Generate another" is clicked.
    expect(screen.queryByRole('button', { name: /generate enrollment token/i })).not.toBeInTheDocument();
  });

  it('surfaces a warning rather than crashing when the endpoint fails', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ message: 'Internal Server Error' }, 500));
    vi.stubGlobal('fetch', fetchMock);

    render(
      <AdminContext>
        <NodeEnroll />
      </AdminContext>,
    );

    await userEvent.click(screen.getByRole('button', { name: /generate enrollment token/i }));

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    // Still on the form, not stuck on a permanently-disabled submit button.
    expect(screen.getByRole('button', { name: /generate enrollment token/i })).toBeEnabled();
  });

  it('falls back to a manual-copy hint when the Clipboard API is unavailable (e.g. non-HTTPS/jsdom)', async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ enrollment_token: 'token-123', expires_at: '2026-07-14T12:00:00Z' }, 201),
    );
    vi.stubGlobal('fetch', fetchMock);
    // jsdom doesn't implement the Clipboard API by default - this asserts
    // NodeEnroll's copy button degrades gracefully rather than throwing
    // (navigator.clipboard is undefined here, same as it would be without
    // "worthy contexts" i.e. plain HTTP - see the component's own copyToken).
    expect(navigator.clipboard).toBeUndefined();

    render(
      <AdminContext>
        <NodeEnroll />
      </AdminContext>,
    );

    await userEvent.click(screen.getByRole('button', { name: /generate enrollment token/i }));
    await screen.findByDisplayValue('token-123');
    await userEvent.click(screen.getByRole('button', { name: /copy token/i }));

    expect(await screen.findByText(/select the token text above/i)).toBeInTheDocument();
  });
});
