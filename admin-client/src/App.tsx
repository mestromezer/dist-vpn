import { Admin, CustomRoutes, Layout, type LayoutProps, Resource } from 'react-admin';
import { Route } from 'react-router-dom';
import DeviceHubIcon from '@mui/icons-material/DeviceHub';
import CallSplitIcon from '@mui/icons-material/CallSplit';
import { dataProvider } from './dataProvider';
import { authProvider } from './auth/authProvider';
import { NodeList, NodeShow } from './resources/nodes';
import { CascadeList, CascadeShow } from './resources/cascades';
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
      <Resource name="nodes" list={NodeList} show={NodeShow} icon={DeviceHubIcon} />
      <Resource name="cascades" list={CascadeList} show={CascadeShow} icon={CallSplitIcon} />
      <CustomRoutes>
        <Route path="/topology" element={<TopologyPage />} />
        <Route path="/change-password" element={<ChangePasswordPage />} />
      </CustomRoutes>
    </Admin>
  );
}

export default App;
