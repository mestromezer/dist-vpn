import { Menu } from 'react-admin';
import AccountTreeIcon from '@mui/icons-material/AccountTree';

/**
 * Default resource menu plus one extra entry linking to the /topology
 * preview route (see topology/TopologyPage.tsx) so it's reachable from the
 * UI instead of only by typing the URL.
 */
export function CustomMenu() {
  return (
    <Menu>
      <Menu.ResourceItems />
      <Menu.Item
        to="/topology"
        primaryText="Topology (preview)"
        leftIcon={<AccountTreeIcon />}
      />
    </Menu>
  );
}
