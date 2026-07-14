import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Box, Button, Card, CardContent, Stack, TextField, Typography } from '@mui/material';
import { HttpError, Title } from 'react-admin';
import { changePassword } from './authProvider';

/**
 * Forced password-change form, routed at /change-password (see App.tsx and
 * PasswordChangeGuard.tsx). Reached right after login when TokenPair.
 * must_change_password is true (TZ-5 §6.3 — deploy-time default password
 * must be changed before doing anything else) and calls
 * POST /auth/change-password. On success, the guard's condition stops
 * holding and the admin can navigate normally.
 */
export function ChangePasswordPage() {
  const navigate = useNavigate();
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = confirmPassword.length > 0 && newPassword !== confirmPassword;

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!currentPassword || !newPassword) {
      setError('Both the current and new password are required.');
      return;
    }
    if (newPassword !== confirmPassword) {
      setError('New password and confirmation do not match.');
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      await changePassword(currentPassword, newPassword);
      navigate('/', { replace: true });
    } catch (err) {
      if (err instanceof HttpError && err.status === 401) {
        setError('Current password is incorrect.');
      } else {
        setError(err instanceof Error ? err.message : 'Failed to change password.');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Box sx={{ display: 'flex', justifyContent: 'center', mt: 6 }}>
      <Title title="Change password required" />
      <Card sx={{ width: 420 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            You must change your password
          </Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            This account is still using a default password issued at deployment
            time. Set a new password to continue.
          </Typography>
          <Box component="form" onSubmit={handleSubmit} noValidate>
            <Stack spacing={2}>
              {error && <Alert severity="error">{error}</Alert>}
              <TextField
                label="Current password"
                type="password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                required
                fullWidth
                autoFocus
              />
              <TextField
                label="New password"
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                required
                fullWidth
              />
              <TextField
                label="Confirm new password"
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                error={mismatch}
                helperText={mismatch ? 'Passwords do not match' : ' '}
                required
                fullWidth
              />
              <Button type="submit" variant="contained" disabled={submitting} fullWidth>
                {submitting ? 'Changing password…' : 'Change password'}
              </Button>
            </Stack>
          </Box>
        </CardContent>
      </Card>
    </Box>
  );
}
