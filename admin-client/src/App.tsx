import { Admin, CustomRoutes, Layout, type LayoutProps, Resource } from 'react-admin';
import { Route } from 'react-router-dom';
import DeviceHubIcon from '@mui/icons-material/DeviceHub';
import { dataProvider } from './dataProvider';
import { authProvider } from './auth/authProvider';
import { NodeList, NodeShow } from './resources/nodes';
import { TopologyPage } from './topology/TopologyPage';
import { CustomMenu } from './CustomMenu';

function CustomLayout(props: LayoutProps) {
  return <Layout {...props} menu={CustomMenu} />;
}

function App() {
  return (
    <Admin dataProvider={dataProvider} authProvider={authProvider} layout={CustomLayout}>
      <Resource name="nodes" list={NodeList} show={NodeShow} icon={DeviceHubIcon} />
      <CustomRoutes>
        <Route path="/topology" element={<TopologyPage />} />
      </CustomRoutes>
    </Admin>
  );
}

export default App;
