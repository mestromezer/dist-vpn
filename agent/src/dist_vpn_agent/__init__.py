"""dist-vpn Agent.

Runs on each VDS node (Russia-entry or foreign-exit) and is the only
component that touches the local Xray-core and Docker directly. This
package currently implements the control-plane gRPC client (Agent<->Platform
dial-out channel, see docs/TZ-4-agent.md and
contracts/proto/agent_control.proto). Xray/Docker management is a follow-up
slice -- see agent/README.md for the current scope.
"""

__version__ = "0.1.0"
