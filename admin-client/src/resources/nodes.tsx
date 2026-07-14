import {
  Datagrid,
  DateField,
  List,
  SelectInput,
  Show,
  SimpleShowLayout,
  TextField,
  useRecordContext,
  type RaRecord,
} from 'react-admin';
import Chip from '@mui/material/Chip';
import type { NodeStatus } from '../contract/types';

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
        <DateField source="last_heartbeat_at" label="Last heartbeat" showTime />
      </SimpleShowLayout>
    </Show>
  );
}
