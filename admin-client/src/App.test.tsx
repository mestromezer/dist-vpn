import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import App from './App';

describe('App', () => {
  it('renders the react-admin login screen when not authenticated', async () => {
    render(<App />);

    // No token in memory on a fresh render, so authProvider.checkAuth()
    // rejects and react-admin redirects to its built-in login screen
    // instead of the nodes list.
    expect(await screen.findByLabelText(/username/i)).toBeInTheDocument();
    // Anchored to the start: the field label renders as "Password *" (MUI's
    // required-field marker), and the show/hide-password icon button has an
    // aria-label of "Show password" — a loose /password/i match would hit
    // both, so anchor to text that only the field label starts with.
    expect(screen.getByLabelText(/^password/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /sign in/i }),
    ).toBeInTheDocument();
  });
});
