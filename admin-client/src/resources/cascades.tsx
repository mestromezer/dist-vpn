import {
  Datagrid,
  FunctionField,
  List,
  Show,
  SimpleShowLayout,
  TextField,
  useRecordContext,
  type RaRecord,
} from 'react-admin';
import Chip from '@mui/material/Chip';
import type { Cascade, CascadeStatus } from '../contract/types';
import { TestRequestButton } from './TestRequestButton';

// exit_node_ids / hop_a_presets are plain string[] in the contract (not
// arrays of {id}-shaped records), so <ArrayField>/<SingleFieldList> (which
// expect record-shaped items) don't apply here — a joined-text
// <FunctionField> is the standard react-admin way to render a primitive
// array without any custom CSS/layout.
function joinedList(values: string[] | undefined): string {
  return values && values.length > 0 ? values.join(', ') : '—';
}

// Same colored-Chip-for-status pattern as resources/nodes.tsx, applied to
// Cascade.status (up/degraded/down) instead of Node.status.
const statusColor: Record<CascadeStatus, 'success' | 'warning' | 'error'> = {
  up: 'success',
  degraded: 'warning',
  down: 'error',
};

interface CascadeStatusFieldProps {
  source?: string;
  label?: string;
}

/** Renders Cascade.status as a colored MUI Chip, per TZ-6 §6 (human-readable
 * statuses, not bare enum text) — same treatment nodes.tsx gives Node.status. */
function CascadeStatusField({ source = 'status' }: CascadeStatusFieldProps) {
  const record = useRecordContext<RaRecord & Record<string, CascadeStatus>>();
  if (!record) return null;
  const status = record[source] as CascadeStatus;
  return (
    <Chip label={status} color={statusColor[status] ?? 'default'} size="small" />
  );
}

export function CascadeList() {
  return (
    <List>
      <Datagrid rowClick="show">
        <TextField source="id" />
        <TextField source="entry_node_id" label="Entry node" />
        <FunctionField<Cascade>
          label="Exit nodes"
          render={(record) => joinedList(record.exit_node_ids)}
        />
        <CascadeStatusField source="status" label="Status" />
      </Datagrid>
    </List>
  );
}

export function CascadeShow() {
  return (
    <Show>
      <SimpleShowLayout>
        <TextField source="id" />
        <TextField source="entry_node_id" label="Entry node" />
        <FunctionField<Cascade>
          label="Exit nodes"
          render={(record) => joinedList(record.exit_node_ids)}
        />
        <CascadeStatusField source="status" label="Status" />
        <FunctionField<Cascade>
          label="Hop A presets"
          render={(record) => joinedList(record.hop_a_presets)}
        />
        <TestRequestButton />
      </SimpleShowLayout>
    </Show>
  );
}
