/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL of the Platform REST API, e.g. http://localhost:8000/api/v1 */
  readonly VITE_API_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
