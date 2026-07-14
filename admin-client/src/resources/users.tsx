import {
  ChipField,
  Create,
  Datagrid,
  DateField,
  DeleteButton,
  List,
  ReferenceArrayField,
  ReferenceArrayInput,
  required,
  SelectArrayInput,
  Show,
  SimpleForm,
  SimpleShowLayout,
  SingleFieldList,
  TextField,
  TextInput,
  useRecordContext,
} from 'react-admin';
import Chip from '@mui/material/Chip';
import type { User } from '../contract/types';

/**
 * Basic <Resource name="users"> (TZ-6 §3.3): list/create/delete against the
 * User schema — enough to add a user, revoke their access, and see the
 * cascades they're attached to. Deliberately does NOT include the usage
 * (/usage) charts TZ-6 §3.3 also asks for — /usage exists in the contract
 * but this slice doesn't wire it up (see README.md TODO); the "consumption"
 * part of the requirement stays a documented gap, not a silent omission.
 */

interface RevokedFieldProps {
  label?: string;
}

// `label` isn't read here — it exists purely so Datagrid/SimpleShowLayout
// (which derive column/row headers from the JSX props on their children,
// the same way nodes.tsx's NodeStatusField does) can pick it up.
function RevokedField(_props: RevokedFieldProps) {
  const record = useRecordContext<User>();
  if (!record) return null;
  return record.revoked_at ? (
    <Chip label={`Revoked ${new Date(record.revoked_at).toLocaleString()}`} color="default" size="small" />
  ) : (
    <Chip label="Active" color="success" size="small" />
  );
}

export function UserList() {
  return (
    <List>
      <Datagrid rowClick="show">
        <TextField source="id" />
        <TextField source="name" />
        <TextField source="uuid" />
        <DateField source="created_at" showTime />
        <RevokedField label="Status" />
        <DeleteButton mutationMode="pessimistic" label="Revoke" />
      </Datagrid>
    </List>
  );
}

export function UserShow() {
  return (
    <Show>
      <SimpleShowLayout>
        <TextField source="id" />
        <TextField source="name" />
        <TextField source="uuid" />
        <DateField source="created_at" showTime />
        <RevokedField label="Status" />
        {/* cascade_ids -> resolved Cascade records, rendered as chips — the
            "see their listed cascades" part of this slice's scope. Requires
            the cascades resource (resources/cascades.tsx) to be registered
            in App.tsx so the dataProvider can resolve these references. */}
        <ReferenceArrayField source="cascade_ids" reference="cascades" label="Cascades">
          <SingleFieldList linkType="show">
            <ChipField source="id" />
          </SingleFieldList>
        </ReferenceArrayField>
      </SimpleShowLayout>
    </Show>
  );
}

export function UserCreate() {
  return (
    <Create redirect="list">
      <SimpleForm>
        <TextInput source="name" validate={required()} />
        {/* Reuses the cascades resource's own list endpoint (GET /cascades)
            for the option set — standard react-admin reference input, no
            custom fetching/validation code. */}
        <ReferenceArrayInput source="cascade_ids" reference="cascades">
          <SelectArrayInput label="Cascades" optionText="id" />
        </ReferenceArrayInput>
      </SimpleForm>
    </Create>
  );
}
