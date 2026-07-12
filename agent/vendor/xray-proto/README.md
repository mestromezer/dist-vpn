# Vendored Xray-core `.proto` files

Source: https://github.com/XTLS/Xray-core, tag `v26.7.11`, commit
`50231eaff98ccc31b5cbd247a721c16e97fe5ec1`. License: MPL-2.0 (see upstream
`LICENSE`; these files are unmodified except for a short provenance comment
prepended to each one -- diff against upstream at the pinned commit to
confirm).

Fetched with `curl` directly from `raw.githubusercontent.com` at the pinned
commit (not retyped/reconstructed from memory), preserving the exact
upstream directory layout so `import "a/b/c.proto"` statements inside them
resolve unchanged. Do not hand-edit; do not add files here by copy-pasting
from docs or memory -- re-fetch from the pinned commit (or a newer one, with
this README and the per-file header comments updated to match) instead.

## Why more than the 3 named command services

TZ-4 SS3.2 names three service packages: `app/proxyman/command`
(`HandlerService`), `app/router/command` (`RoutingService`),
`app/stats/command` (`StatsService`). Those `.proto` files alone are enough
to generate working gRPC *stubs* for the three services, but not enough to
*construct valid request messages* for `AddInbound`/`AddOutbound`/`AddRule`:

- `HandlerService.AddInbound`'s request wraps a `core.InboundHandlerConfig`,
  whose `receiver_settings`/`proxy_settings` fields are
  `xray.common.serial.TypedMessage` (a type-name + serialized-bytes
  envelope, Xray's home-grown equivalent of `google.protobuf.Any`). To
  populate that envelope with something Xray will actually accept, the
  *actual* proxy/transport-specific settings message types are needed, not
  just the envelope shape.
- `RoutingService.AddRule`'s request wraps a `xray.app.router.RoutingRule`
  the same way, so `app/router/config.proto` (not just
  `app/router/command/command.proto`) is needed too.

This project's scope (TZ-4 SS3.3: presets P1-P4, all VLESS) bounds which
proxy/transport types are needed -- vendoring is deliberately **not** "every
proxy Xray supports", only what backs the four presets plus the plain
`freedom` outbound used for local egress:

| File | Needed for |
|---|---|
| `app/proxyman/command/command.proto` | `HandlerService` (the 3 named services) |
| `app/router/command/command.proto` | `RoutingService` |
| `app/stats/command/command.proto` | `StatsService` (message shapes only; not called yet -- see agent/README.md) |
| `app/proxyman/config.proto` | `ReceiverConfig`/`SenderConfig` -- the actual payload of `InboundHandlerConfig.receiver_settings` / `OutboundHandlerConfig.sender_settings` |
| `app/router/config.proto` | `RoutingRule`, the payload of `AddRuleRequest.config` |
| `core/config.proto` | `InboundHandlerConfig` / `OutboundHandlerConfig` themselves |
| `proxy/vless/inbound/config.proto`, `proxy/vless/account.proto` | VLESS is the only inbound protocol these presets use (P1-P4, TZ-4 SS3.3); `Account` is also `User.account`'s payload for `AddUserOperation` |
| `proxy/freedom/config.proto` | Plain pass-through outbound used for local egress |
| `transport/internet/config.proto` | `StreamConfig`/`TransportConfig`, tying a stream's transport+security settings together |
| `transport/internet/tls/config.proto`, `transport/internet/reality/config.proto` | Security settings for P1-P4 (REALITY on raw/XHTTP/gRPC, TLS on WebSocket) |
| `transport/internet/grpc/config.proto`, `transport/internet/splithttp/config.proto`, `transport/internet/websocket/config.proto`, `transport/internet/tcp/config.proto` | The four transports named in P1-P4 (gRPC, XHTTP -- upstream package name is still `splithttp` --, WebSocket, raw/TCP) |
| `common/net/*.proto`, `common/protocol/*.proto`, `common/serial/typed_message.proto`, `common/geodata/geodat.proto` | Transitively required by the above (addresses, ports, the generic `User`/`ServerEndpoint`, the `TypedMessage` envelope itself, domain/IP matchers used by `RoutingRule`) |

If a non-VLESS proxy or a fifth transport is ever needed, vendor its
`.proto` the same way (fetch from the pinned/newer commit, preserve upstream
path, add the provenance header, add it to `agent/scripts/gen_proto.sh`'s
file list) rather than guessing its shape.

## Regenerating stubs

See `agent/scripts/gen_proto.sh` -- it compiles both this tree and
`contracts/proto/agent_control.proto` in one run. Nothing under
`src/dist_vpn_agent/generated/` (including the `xray/` subtree generated
from here) is committed; see the repo's root `.gitignore`.
