/**
 * TypeScript mirrors of the schemas in `contracts/rest/openapi.yaml` that this
 * slice actually uses (auth + Node + Cascade + LinkTest + User). Hand-transcribed
 * rather than code-generated for this first slice — see admin-client/README.md
 * "Scope of this slice" for the tradeoff and the follow-up (contracts/README.md
 * suggests generating a typed client straight from the OpenAPI schema once more
 * resources land).
 *
 * Keep in sync with contracts/rest/openapi.yaml. Do not edit the contract from
 * here — if the shape needs to change, that's a cross-team contract change.
 *
 * Contract-drift note (resolved): an earlier revision of this branch was
 * forked before `main` picked up commits `2bac197` and `d3055d7`, which add
 * `TokenPair.must_change_password`, `POST /auth/change-password`, the
 * optional `refresh_token` body on `POST /auth/logout`, and `total` on the
 * `/cascades`/`/users` list envelopes. This branch has since merged `main`
 * (see git log — `admin-client/scaffold` now contains those two commits), so
 * `contracts/rest/openapi.yaml` in this branch and the types below agree —
 * no more drift to track here. Kept as a note in case a future rebase
 * reintroduces a gap; check `git log main -- contracts/rest/openapi.yaml`
 * against this branch's own history if types here ever look stale again.
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
  /**
   * True when the password is a deploy-time default and must be changed
   * before the admin can do anything else (TZ-5 §6.3). Upstream contract
   * addition — see the file-level "contract-drift note" above.
   */
  must_change_password?: boolean;
}

export interface LoginRequest {
  username: string;
  password: string;
  /** TOTP code, present only when MFA is enabled on the Platform. */
  totp_code?: string;
}

export interface ChangePasswordRequest {
  current_password: string;
  new_password: string;
}

export type CascadeStatus = 'up' | 'degraded' | 'down';

export interface Cascade {
  id: string;
  entry_node_id: string;
  exit_node_ids: string[];
  status: CascadeStatus;
  hop_a_presets?: string[];
}

export interface HealthEvent {
  id: string;
  cascade_id: string;
  node_id: string;
  from_status: string;
  to_status: string;
  reason: string;
  occurred_at: string;
}

export type TestOutcome =
  | 'success'
  | 'container_start_failed'
  | 'tls_handshake_failed'
  | 'proxy_no_response'
  | 'timeout';

export interface LinkTest {
  id: string;
  cascade_id: string;
  /** Absent while the test is still running (see testRequest.ts polling). */
  outcome?: TestOutcome;
  latency_ms?: number;
  detail?: string;
  initiated_by?: string;
  created_at?: string;
}

export interface User {
  id: string;
  name: string;
  /** Personal VLESS identifier; stable across server rotation. */
  uuid: string;
  cascade_ids: string[];
  created_at: string;
  revoked_at?: string | null;
}
