import { Admin, CustomRoutes, Layout, type LayoutProps, Resource } from 'react-admin';
import { Route } from 'react-router-dom';
import DeviceHubIcon from '@mui/icons-material/DeviceHub';
import CallSplitIcon from '@mui/icons-material/CallSplit';
import PeopleIcon from '@mui/icons-material/People';
import { dataProvider } from './dataProvider';
import { authProvider } from './auth/authProvider';
import { NodeEdit, NodeEnroll, NodeList, NodeShow } from './resources/nodes';
import { CascadeList, CascadeShow } from './resources/cascades';
import { UserCreate, UserList, UserShow } from './resources/users';
import { TopologyPage } from './topology/TopologyPage';
import { ChangePasswordPage } from './auth/ChangePasswordPage';
import { PasswordChangeGuard } from './auth/PasswordChangeGuard';
import { CustomMenu } from './CustomMenu';

function CustomLayout(props: LayoutProps) {
  return (
    <>
      <PasswordChangeGuard />
      <Layout {...props} menu={CustomMenu} />
    </>
  );
}

function App() {
  return (
    <Admin dataProvider={dataProvider} authProvider={authProvider} layout={CustomLayout}>
      <Resource
        name="nodes"
        list={NodeList}
        show={NodeShow}
        edit={NodeEdit}
        create={NodeEnroll}
        icon={DeviceHubIcon}
      />
      <Resource name="cascades" list={CascadeList} show={CascadeShow} icon={CallSplitIcon} />
      <Resource
        name="users"
        list={UserList}
        show={UserShow}
        create={UserCreate}
        icon={PeopleIcon}
      />
      <CustomRoutes>
        <Route path="/topology" element={<TopologyPage />} />
        <Route path="/change-password" element={<ChangePasswordPage />} />
      </CustomRoutes>
    </Admin>
  );
}

export default App;
