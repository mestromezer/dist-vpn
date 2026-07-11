/**
 * TypeScript mirrors of the schemas in `contracts/rest/openapi.yaml` that this
 * slice actually uses (auth + Node). Hand-transcribed rather than code-generated
 * for this first slice — see admin-client/README.md "Scope of this slice" for
 * the tradeoff and the follow-up (contracts/README.md suggests generating a
 * typed client straight from the OpenAPI schema once more resources land).
 *
 * Keep in sync with contracts/rest/openapi.yaml. Do not edit the contract from
 * here — if the shape needs to change, that's a cross-team contract change.
 */

export type NodeRole = 'entry' | 'exit';

export type NodeStatus =
  | 'enrolling'
  | 'healthy'
  | 'degraded'
  | 'down'
  | 'decommissioned';

export interface Node {
  id: string;
  label: string;
  role: NodeRole;
  region: string;
  status: NodeStatus;
  agent_version: string;
  last_heartbeat_at: string;
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  /** Seconds until the access token expires (15-30 min per TZ-5 §6.3). */
  expires_in: number;
}

export interface LoginRequest {
  username: string;
  password: string;
  /** TOTP code, present only when MFA is enabled on the Platform. */
  totp_code?: string;
}
