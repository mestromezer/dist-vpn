import { useState } from 'react';
import {
  Datagrid,
  DateField,
  DeleteButton,
  Edit,
  List,
  required,
  SelectInput,
  Show,
  SimpleForm,
  SimpleShowLayout,
  TextField,
  TextInput,
  useRecordContext,
  type RaRecord,
} from 'react-admin';
import { Alert, Box, Button, Stack, TextField as MuiTextField, Typography } from '@mui/material';
import Chip from '@mui/material/Chip';
import type { Node, NodeRole, NodeStatus } from '../contract/types';
import { authorizedFetchJson } from '../auth/httpClient';

// Contract enums (NodeRole / NodeStatus in openapi.yaml) — kept as
// `<SelectInput>` choices so filtering stays a built-in react-admin
// component, not a hand-rolled control.
const roleChoices = [
  { id: 'entry', name: 'entry' },
  { id: 'exit', name: 'exit' },
];

const statusChoices: { id: NodeStatus; name: string }[] = [
  { id: 'enrolling', name: 'enrolling' },
  { id: 'healthy', name: 'healthy' },
  { id: 'degraded', name: 'degraded' },
  { id: 'down', name: 'down' },
  { id: 'decommissioned', name: 'decommissioned' },
];

// GET /nodes only supports `role` and `status` query filters per the
// contract (no free-text search param), so those are the only filters
// offered here.
const nodeFilters = [
  <SelectInput key="role" source="role" choices={roleChoices} alwaysOn />,
  <SelectInput key="status" source="status" choices={statusChoices} alwaysOn />,
];

const statusColor: Record<
  NodeStatus,
  'success' | 'warning' | 'error' | 'default' | 'info'
> = {
  healthy: 'success',
  degraded: 'warning',
  down: 'error',
  decommissioned: 'default',
  enrolling: 'info',
};

interface NodeStatusFieldProps {
  source?: string;
  label?: string;
}

/** Renders Node.status as a colored MUI Chip instead of raw text, per the
 * TZ-6 §6 requirement for human-readable statuses (not bare enum values).
 * Accepts the usual `source`/`label` field props so Datagrid/SimpleShowLayout
 * can still derive column headers and labels from it like any other field. */
function NodeStatusField({ source = 'status' }: NodeStatusFieldProps) {
  const record = useRecordContext<RaRecord & Record<string, NodeStatus>>();
  if (!record) return null;
  const status = record[source] as NodeStatus;
  return (
    <Chip label={status} color={statusColor[status] ?? 'default'} size="small" />
  );
}

interface DecommissionButtonProps {
  label?: string;
}

/** DELETE /nodes/{id} marks the node DECOMMISSIONED server-side (see
 * platform/api/routes/nodes.py::decommission_node) rather than actually
 * deleting the row - `<DeleteButton>` still fits, matching users.tsx's
 * "Revoke" button (`DELETE /users/{id}` doesn't hard-delete either), and
 * `dataProvider.delete` already tolerates a 202-with-no-record response by
 * falling back to `previousData`. Renders nothing once a node is already
 * decommissioned, rather than offering an action that would just 404/no-op
 * again on an already-terminal state.
 *
 * `label` isn't read here (same reasoning as NodeStatusField/RevokedField
 * above) - it exists purely so Datagrid can derive this column's header
 * from the JSX prop on the element it renders. */
function DecommissionButton(_props: DecommissionButtonProps) {
  const record = useRecordContext<Node>();
  if (!record || record.status === 'decommissioned') return null;
  return <DeleteButton mutationMode="pessimistic" label="Decommission" />;
}

export function NodeList() {
  return (
    <List filters={nodeFilters}>
      <Datagrid rowClick="show">
        <TextField source="id" />
        <TextField source="label" />
        <TextField source="role" />
        <TextField source="region" />
        <NodeStatusField source="status" label="Status" />
        <TextField source="agent_version" label="Agent version" />
        <DateField source="last_heartbeat_at" label="Last heartbeat" showTime />
        <DecommissionButton label="Decommission" />
      </Datagrid>
    </List>
  );
}

export function NodeShow() {
  return (
    <Show>
      <SimpleShowLayout>
        <TextField source="id" />
        <TextField source="label" />
        <TextField source="role" />
        <TextField source="region" />
        <NodeStatusField source="status" label="Status" />
        <TextField source="agent_version" label="Agent version" />
        <TextField source="public_address" label="Public address" emptyText="— not set —" />
        <DateField source="last_heartbeat_at" label="Last heartbeat" showTime />
      </SimpleShowLayout>
    </Show>
  );
}

/**
 * `PATCH /nodes/{id}` (role/label/public_address) fits react-admin's normal
 * Edit/dataProvider.update flow directly - unlike NodeEnroll below, the
 * response is a full Node record with the same id, so this is a plain
 * <Edit>/<SimpleForm>, the same shape as any other resource's edit view.
 */
export function NodeEdit() {
  return (
    <Edit>
      <SimpleForm>
        <TextField source="id" />
        <TextInput source="label" />
        <SelectInput source="role" choices={roleChoices} validate={required()} />
        {/* Required (only) to compose a client vless:// URI for an entry
            node (see api/routes/users.py's connection endpoint on Platform)
            - the operator is the only one who knows the VDS's real public
            IP/hostname, so this is never inferred, only admin-set. */}
        <TextInput source="public_address" label="Public address" helperText="IP or hostname clients should dial (entry nodes only)" />
      </SimpleForm>
    </Edit>
  );
}

const enrollRoleChoices: { id: NodeRole; label: string }[] = [
  { id: 'entry', label: 'entry' },
  { id: 'exit', label: 'exit' },
];

/**
 * "Add node to pool" (TZ-6 §3.1) — deliberately not a react-admin
 * <Create>/<SimpleForm>: `POST /nodes` doesn't create a Node record at all,
 * it mints a one-time enrollment token (platform/api/routes/nodes.py::
 * create_enrollment_token) that an operator pastes into the VDS
 * provisioning step (provisioning/README.md); the actual Node row only
 * appears later, once an Agent enrolls with that token over gRPC. There is
 * no `id` to redirect to and nothing yet to show on a record page, so this
 * skips dataProvider.create/<Create> entirely and talks to the endpoint
 * directly - same "custom fetch when the REST shape doesn't fit react-admin's
 * CRUD assumptions" precedent as TestRequestButton.tsx/TestHistoryList.tsx.
 * Still registered as the `create` prop on `<Resource name="nodes">` so
 * react-admin's own list-page "Create" button links here for free.
 */
export function NodeEnroll() {
  const [role, setRole] = useState<NodeRole>('entry');
  const [label, setLabel] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [token, setToken] = useState<{ enrollment_token: string; expires_at: string } | null>(null);
  const [copyFailed, setCopyFailed] = useState(false);

  const submit = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const { body } = await authorizedFetchJson<{ enrollment_token: string; expires_at: string }>('/nodes', {
        method: 'POST',
        body: JSON.stringify({ requested_role: role, label: label.trim() || undefined }),
      });
      setToken(body);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate an enrollment token.');
    } finally {
      setSubmitting(false);
    }
  };

  const copyToken = () => {
    if (!token) return;
    setCopyFailed(false);
    if (!navigator.clipboard) {
      setCopyFailed(true);
      return;
    }
    navigator.clipboard.writeText(token.enrollment_token).catch(() => setCopyFailed(true));
  };

  return (
    <Box component="section" sx={{ p: 3, maxWidth: 480 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        Add node to pool
      </Typography>
      {!token && (
        <Stack spacing={2}>
          <MuiTextField
            select
            label="Role"
            value={role}
            onChange={(e) => setRole(e.target.value as NodeRole)}
            slotProps={{ select: { native: true } }}
          >
            {enrollRoleChoices.map((choice) => (
              <option key={choice.id} value={choice.id}>
                {choice.label}
              </option>
            ))}
          </MuiTextField>
          <MuiTextField
            label="Label"
            helperText="Optional human-readable name, e.g. ru-msk-1"
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          {error && <Alert severity="error">{error}</Alert>}
          <Button variant="contained" onClick={submit} disabled={submitting}>
            {submitting ? 'Generating…' : 'Generate enrollment token'}
          </Button>
        </Stack>
      )}
      {token && (
        <Stack spacing={2}>
          <Alert severity="warning">
            This token is shown once and is never stored by Platform — copy it now. Paste it as{' '}
            <code>enrollment_token</code> for the provisioning role on the new VDS (see
            provisioning/README.md).
          </Alert>
          <MuiTextField
            label="Enrollment token"
            value={token.enrollment_token}
            slotProps={{ input: { readOnly: true } }}
            multiline
          />
          <Typography variant="body2" color="text.secondary">
            Expires at {new Date(token.expires_at).toLocaleString()}
          </Typography>
          <Stack direction="row" spacing={2}>
            <Button variant="outlined" onClick={copyToken}>
              Copy token
            </Button>
            <Button
              onClick={() => {
                setToken(null);
                setLabel('');
              }}
            >
              Generate another
            </Button>
          </Stack>
          {copyFailed && (
            <Alert severity="info">Clipboard access isn't available — select the token text above and copy it manually.</Alert>
          )}
        </Stack>
      )}
    </Box>
  );
}
