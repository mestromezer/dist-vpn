import { useEffect } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { mustChangePassword } from './tokenStore';

const CHANGE_PASSWORD_PATH = '/change-password';

/**
 * Renders nothing; forces navigation to the change-password screen whenever
 * tokenStore.mustChangePassword() is true and the admin isn't already there.
 *
 * Why this exists in addition to authProvider.login()'s `redirectTo`: that
 * only fires once, immediately after login. If must_change_password is still
 * true (the admin hasn't completed the change yet) and they navigate
 * elsewhere — a bookmarked deep link, the browser back button, clicking a
 * menu item before the change-password form is submitted — this guard sends
 * them back. Mounted once in CustomLayout (src/App.tsx) so it sees every
 * route change inside the authenticated app shell.
 */
export function PasswordChangeGuard() {
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (mustChangePassword() && location.pathname !== CHANGE_PASSWORD_PATH) {
      navigate(CHANGE_PASSWORD_PATH, { replace: true });
    }
  }, [location, navigate]);

  return null;
}
