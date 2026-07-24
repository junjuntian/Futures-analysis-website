# V2BX external core rewrite design

版本：2026-07-07 review-1

目标：把 V2BX-malio 改造成 SSPanel 控制面和外部官方 Core 编排器，彻底移除 Xray、Sing-box、Hysteria2 的 Go module 内嵌依赖。

本文是开发前设计锁定文档，不包含 Go 代码实现。

## 0. Evidence and status legend

能力判断必须来自官方文档或官方源码。本文使用的证据：

- Xray official source, local module cache `github.com/xtls/xray-core@v1.260327.0`:
  - `app/stats/command/command.proto`
  - `app/stats/command/command.go`
  - `app/proxyman/command/command.proto`
  - `app/dispatcher/default.go`
  - `infra/conf/policy.go`
- Sing-box official source, SagerNet tag `v1.13.14`:
  - `experimental/v2rayapi/stats.go`
  - `protocol/anytls/inbound.go`
  - `protocol/hysteria2/inbound.go`
- Official docs:
  - Xray API, Stats, Policy docs: https://xtls.github.io/en/config/api.html, https://xtls.github.io/en/config/stats.html, https://xtls.github.io/en/config/policy.html
  - Sing-box V2Ray API docs: https://sing-box.sagernet.org/configuration/experimental/v2ray-api/
  - Sing-box AnyTLS inbound docs: https://sing-box.sagernet.org/configuration/inbound/anytls/
  - Sing-box Hysteria2 inbound docs: https://sing-box.sagernet.org/configuration/inbound/hysteria2/
  - Xray releases: https://github.com/XTLS/Xray-core/releases
  - Sing-box releases: https://github.com/SagerNet/sing-box/releases

Capability levels:

- `full`: 官方文档或官方源码支持，控制面可以不嵌入 Core 实现该能力。
- `degraded`: 可以保留部分业务效果，但精度、实时性或故障语义低于旧内嵌 hook。
- `unsupported`: 无证据表明官方二进制提供该能力，且不允许重新内嵌 Core 恢复。
- `待验证`: 需要用你的 SSPanel 真实字段、订阅链接或目标二进制实测确认，不能作为开发假设。

## A. Review findings on previous design

1. CoreName/CoreType 关系不清。
   Previous text used `Node.Core = "xray"` while also defining `Cores[].Type` and `Cores[].Name`. That makes it unclear whether a node selects a process instance or a core implementation. Revised rule: `CoreSpec.Type` is implementation type (`xray` or `sing`), `CoreSpec.Name` is the process instance, and `Node.CoreName` must reference `CoreSpec.Name`.

2. Process topology was ambiguous.
   Previous text did not state whether one core process serves one node or many nodes. Revised rule: one `CoreSpec.Name` owns one external process and one rendered config file. That process can contain multiple node inbounds. Multiple processes of the same type are allowed only by defining multiple `CoreSpec` entries.

3. Sing-box stats were overstated.
   Official source confirms Sing-box V2Ray API has `GetStats`, `QueryStats`, `GetSysStats`, and reset support. 无证据表明 Sing-box V2Ray API exposes online IP, active connection list, user device count, or dynamic user mutation API. Any statement implying universal Sing-box online IP or device limit support is removed.

4. Xray stats boundary needed exact wording.
   Xray source confirms `StatsService.GetStats` and `QueryStats` support `reset`; `StatsService.GetStatsOnlineIpList` and `GetAllOnlineUsers` exist in `v1.260327.0`; dispatcher records online source IP per `user.Email` when `statsUserOnline` is enabled. This supports VLESS Reality user traffic and online IP with Xray, but it does not provide user speed limiting.

5. Hysteria2 authentication mapping was incomplete.
   Sing-box Hysteria2 docs state that official Hysteria2 `userpass` is not an alias in Sing-box; if using that official program format, `<username>:<password>` must be filled as the actual password. The previous design did not define how SSPanel maps to this. Revised rule: for this SSP panel contract, `users[].name = uuid` and `users[].password = uuid` unless a real subscription fixture proves the panel emits a different Hy2 password. This is a pre-development blocker.

6. Hysteria2 obfs mapping had a conflict with current code.
   Current V2BX-malio parser defaults `obfs=plain`, while Sing renderer can treat a non-empty obfs value with empty password as `salamander` password. Official Sing-box docs only define `obfs.type=salamander` with a password. Revised rule: `plain`, `none`, or empty means omit `obfs`; `salamander` requires `obfs_password`. One-field legacy behavior is `待验证`.

7. AnyTLS and Hysteria2 version gates were missing.
   Official Sing-box docs mark AnyTLS as since sing-box `1.12.0`. Hysteria2 docs show relevant option changes in `1.11.0`, but this project should use Sing-box `>= 1.12.0` because AnyTLS is mandatory. Recommended verified target is Sing-box `v1.13.14`; later versions require acceptance tests before production.

8. Traffic idempotency was not strong enough.
   The previous reset-after-report design can lose or duplicate traffic if V2BX crashes between core reset and panel commit. Revised rule: default accounting uses non-reset snapshots plus a local journal. Reset API remains available but is not the default path.

9. Panel empty user list behavior was unsafe.
   A successful empty list can be legitimate, but an API or serialization issue can also produce empty data. Revised rule: never delete all users on a single empty response by default; require consecutive confirmed empty snapshots or an explicit operator policy.

10. Rollback needed a formal state machine.
    Revised design includes apply, health check, and rollback states, including pre-stop traffic flush before a controlled core restart.

## 1. Goals

The rewritten service is a control plane only:

```text
SSPanel
  |
  v
V2BX external-core control plane
  - SSPanel mod_mu API compatibility
  - node sync
  - user sync
  - subscription field compatibility
  - official JSON config generation
  - traffic accounting journal
  - online IP reporting where officially supported
  - process lifecycle management
  - last-good config rollback
  |
  +-- official xray binary
  +-- official sing-box binary
```

Hard goals:

- no `github.com/xtls/xray-core` dependency in control-plane `go.mod`;
- no `github.com/sagernet/sing-box` or fork dependency in control-plane `go.mod`;
- no `github.com/apernet/hysteria` dependency in control-plane `go.mod`;
- SSPanel `mod_mu` API remains compatible;
- VLESS Reality, AnyTLS, and Hysteria2 subscription fields remain compatible, so users do not re-import;
- traffic stats are correct for controlled restarts and avoid double counting;
- config errors roll back to `last-good`;
- Core upgrades are binary replacement plus health checks, not V2BX recompilation.

Non-goals:

- Do not migrate legacy rc4-md5 Shadowsocks users in this rewrite.
- Do not rewrite SSPanel subscription generation.
- Do not recover old hook behavior by embedding Xray, Sing-box, or Hysteria2.

## 2. Target repository and dependency boundary

Recommended repository: new repo, for example `junjuntian/ssp-node-orchestrator` or `junjuntian/v2bx-external-core`.

Allowed control-plane dependencies:

- HTTP client and JSON packages.
- gRPC client packages and generated protobuf code for Xray/Sing StatsService and Xray HandlerService, if generated from proto definitions into this project. This is not Core embedding.
- OS process, file, and systemd integration packages.

Forbidden dependencies:

- `github.com/xtls/xray-core`
- `github.com/sagernet/sing-box`
- `github.com/wyx2685/sing-box_mod`
- `github.com/apernet/hysteria/core/v2`
- any package that starts protocol engines in-process.

## 3. Configuration model

### 3.1 CoreSpec

```json
{
  "Cores": [
    {
      "Name": "xray-main",
      "Type": "xray",
      "Binary": "/usr/local/bin/xray",
      "WorkDir": "/etc/v2bx-control/runtime/xray-main",
      "APIBind": "127.0.0.1:10085",
      "MinVersion": "v1.260327.0",
      "VerifiedVersion": "v1.260327.0",
      "RestartPolicy": "restart",
      "HealthTimeout": "10s"
    },
    {
      "Name": "sing-main",
      "Type": "sing",
      "Binary": "/usr/local/bin/sing-box",
      "WorkDir": "/etc/v2bx-control/runtime/sing-main",
      "APIBind": "127.0.0.1:10086",
      "MinVersion": "1.12.0",
      "VerifiedVersion": "1.13.14",
      "RestartPolicy": "restart",
      "HealthTimeout": "10s"
    }
  ]
}
```

Rules:

- `Name` is unique and is the process identity.
- `Type` selects renderer and process adapter.
- One `CoreSpec` equals one external process, one config file, one pid file, and one last-good config.
- Multiple nodes can share the same `CoreSpec`.
- Multiple processes of the same type are allowed by creating multiple `CoreSpec` entries, for example `sing-hy2` and `sing-anytls`.

### 3.2 NodeSpec

```json
{
  "Nodes": [
    {
      "CoreName": "xray-main",
      "ApiConfig": {
        "ApiHost": "https://example.com",
        "ApiKey": "secret",
        "NodeID": 62,
        "NodeType": "vless",
        "Timeout": 30
      },
      "Options": {
        "ListenIP": "0.0.0.0",
        "CertConfig": {
          "CertMode": "file",
          "CertFile": "/etc/v2bx-control/cert/fullchain.pem",
          "KeyFile": "/etc/v2bx-control/cert/privkey.pem"
        },
        "LimitPolicy": {
          "DeviceLimitMode": "monitor",
          "SpeedLimitMode": "unsupported"
        }
      }
    }
  ]
}
```

Compatibility rule:

- Old `Node.Core = "xray"` or `"sing"` may be accepted only if there is exactly one core with that `Type`. Otherwise startup must fail with a clear error and require `CoreName`.

## 4. Runtime files

Default Linux layout:

```text
/etc/v2bx-control/
  config.json
  cert/
  runtime/
    state.db
    xray-main/
      config.json
      config.next.json
      last-good.json
      xray.pid
      apply.lock
    sing-main/
      config.json
      config.next.json
      last-good.json
      sing-box.pid
      apply.lock
  logs/
    v2bx-control.log
    xray-main.log
    sing-main.log
/usr/local/bin/
  v2bx-control
  xray
  sing-box
```

All paths are configurable.

## 5. Package layout

```text
cmd/
  server.go
internal/
  config/
  panel/
    sspanel/
  controller/
    node_sync.go
    user_sync.go
    report_loop.go
  model/
    desired_state.go
    capability.go
  render/
    xray/
    sing/
  process/
    manager.go
    health.go
    rollback.go
  stats/
    ledger.go
    xray_client.go
    sing_client.go
  online/
    xray_online.go
    best_effort.go
  install/
  security/
docs/
```

Boundary:

- `panel`: SSPanel API DTOs and compatibility.
- `controller`: polling, desired-state diff, report scheduling.
- `render`: official JSON only.
- `process`: external binary lifecycle only.
- `stats`: official API clients and local accounting journal.
- `online`: only official online APIs or explicitly degraded collectors.

## 6. SSPanel mod_mu API contract

### 6.1 Read node

Request:

```text
GET /mod_mu/nodes/{node_id}/info?key={api_key}
```

Expected response:

```json
{
  "ret": 1,
  "data": {
    "node_group": 0,
    "node_class": 0,
    "node_speedlimit": 0,
    "traffic_rate": 1.0,
    "mu_only": 0,
    "sort": 16,
    "server": "host;port=443&flow=xtls-rprx-vision&security=reality&dest=www.microsoft.com:443&serverName=www.microsoft.com&privateKey=xxx&shortId=xxx",
    "type": "vless",
    "online": 0
  }
}
```

### 6.2 Read users

Request:

```text
GET /mod_mu/users?key={api_key}&node_id={node_id}
```

Relevant fields:

```json
{
  "id": 10001,
  "uuid": "user-uuid",
  "node_speedlimit": 0,
  "node_connector": 0,
  "u": 0,
  "d": 0
}
```

Control-plane user model:

```text
User.ID          <- id
User.UUID        <- uuid
User.SpeedLimit  <- node_speedlimit
User.DeviceLimit <- node_connector
```

### 6.3 Report traffic

Request:

```text
POST /mod_mu/users/traffic?key={api_key}&node_id={node_id}
Content-Type: application/json

{
  "data": [
    { "user_id": 10001, "u": 123, "d": 456 }
  ]
}
```

Compatibility:

- Payload shape must remain unchanged by default.
- Optional future extension: add `report_id` only after confirming the panel ignores unknown fields or supports idempotency. Without this, exact idempotency after unknown HTTP outcome is impossible.

### 6.4 Report online IP

Request:

```text
POST /mod_mu/users/aliveip?key={api_key}&node_id={node_id}
Content-Type: application/json

{
  "data": [
    { "user_id": 10001, "ip": "203.0.113.10" }
  ]
}
```

Compatibility:

- Only report records whose user identity is backed by official API evidence.
- For protocols without accurate user-to-IP evidence, send no record by default and log capability `online_ip=unsupported`.

### 6.5 Report node status

Request:

```text
POST /mod_mu/nodes/{node_id}/info?key={api_key}
Form:
  load=<load-string>
  uptime=<seconds>
```

This remains control-plane only and does not depend on protocol cores.

## 7. SSPanel field to core JSON mapping

### 7.1 Common parsing

`server` format:

```text
host;key=value&key=value
host;key=value|key=value
```

Rules:

- `host` is the public hostname used by subscription and listen identity.
- `port` defaults to `443` only if absent.
- All unknown key-value pairs are preserved in `RawParams` for fixture debugging.
- Renderer must fail on required missing fields; it must not invent Reality private keys or user passwords.

Node tag format:

```text
node-{node_id}-{protocol}
```

User stats name format:

```text
VLESS/Xray email: {nodeTag}|{uuid}
Sing user name:   {uuid}
```

The control plane maps both forms back to `user_id` through the user table.

### 7.2 VLESS Reality, SSPanel sort 16

SSPanel fields:

| SSPanel source | Meaning | Xray JSON |
| --- | --- | --- |
| `server` host | public host | not directly used by Xray listen; retained for subscriptions and logs |
| `port` | listen port | `inbounds[].port` |
| `flow` | VLESS flow | `settings.clients[].flow` |
| `security=reality` | Reality marker | `streamSettings.security="reality"` |
| `dest` | Reality handshake target | `streamSettings.realitySettings.dest` |
| `serverName` | Reality SNI list | `streamSettings.realitySettings.serverNames[]` |
| `privateKey` | Reality private key | `streamSettings.realitySettings.privateKey` |
| `shortId` | Reality short id | `streamSettings.realitySettings.shortIds[]` |
| `xver` | PROXY protocol version | `streamSettings.realitySettings.xver`, default `0` |
| `minClientVer` | Reality min client | `streamSettings.realitySettings.minClientVer`, optional |
| `maxClientVer` | Reality max client | `streamSettings.realitySettings.maxClientVer`, optional |
| `maxTimeDiff` | max time diff | `streamSettings.realitySettings.maxTimeDiff`, optional, unit must be validated against Xray JSON schema |
| user `uuid` | VLESS UUID | `settings.clients[].id` |
| user `uuid` + node tag | stats identity | `settings.clients[].email = "{nodeTag}|{uuid}"` |

Required Xray global config:

```json
{
  "stats": {},
  "policy": {
    "levels": {
      "0": {
        "statsUserUplink": true,
        "statsUserDownlink": true,
        "statsUserOnline": true
      }
    }
  },
  "api": {
    "tag": "api",
    "services": ["StatsService", "HandlerService"]
  }
}
```

API inbound must bind to loopback only.

### 7.3 AnyTLS, SSPanel sort 18

SSPanel fields:

| SSPanel source | Meaning | Sing-box JSON |
| --- | --- | --- |
| `server` host | public host | retained for logs and optional TLS `server_name` if valid |
| `port` | listen port | `inbounds[].listen_port` |
| `server_name` | subscription SNI | `inbounds[].tls.server_name` only if official schema accepts it; otherwise certificate/SNI behavior is controlled by TLS certificate |
| `padding_scheme` | URL-encoded JSON string array | `inbounds[].padding_scheme` |
| user `uuid` | auth password | `inbounds[].users[].password` |
| user `uuid` | stats identity | `inbounds[].users[].name` and `experimental.v2ray_api.stats.users[]` |

Renderer output:

```json
{
  "type": "anytls",
  "tag": "node-18-anytls",
  "listen": "0.0.0.0",
  "listen_port": 443,
  "users": [
    { "name": "uuid", "password": "uuid" }
  ],
  "padding_scheme": ["stop=8"],
  "tls": {
    "enabled": true,
    "certificate_path": "/path/fullchain.pem",
    "key_path": "/path/privkey.pem"
  }
}
```

AnyTLS minimum version:

- Official docs: since Sing-box `1.12.0`.
- Project minimum: Sing-box `>= 1.12.0`.
- Recommended verified version: Sing-box `1.13.14`.

### 7.4 Hysteria2, SSPanel sort 17

SSPanel fields:

| SSPanel source | Meaning | Sing-box JSON |
| --- | --- | --- |
| `server` host | public host | retained for subscriptions and logs |
| `port` | listen UDP port | `inbounds[].listen_port` |
| `up_mbps` | node upload bandwidth | `inbounds[].up_mbps` |
| `down_mbps` | node download bandwidth | `inbounds[].down_mbps` |
| `obfs=salamander` | enable QUIC obfs | `inbounds[].obfs.type="salamander"` |
| `obfs_password` | obfs password | `inbounds[].obfs.password` |
| `obfs=plain`, `none`, empty | no obfs | omit `obfs` |
| `ignore_client_bandwidth` | Hy2 bandwidth behavior | `inbounds[].ignore_client_bandwidth` |
| user `uuid` | auth password | `inbounds[].users[].password` |
| user `uuid` | stats identity | `inbounds[].users[].name` and `experimental.v2ray_api.stats.users[]` |

Renderer output:

```json
{
  "type": "hysteria2",
  "tag": "node-17-hy2",
  "listen": "0.0.0.0",
  "listen_port": 443,
  "up_mbps": 100,
  "down_mbps": 100,
  "users": [
    { "name": "uuid", "password": "uuid" }
  ],
  "tls": {
    "enabled": true,
    "certificate_path": "/path/fullchain.pem",
    "key_path": "/path/privkey.pem"
  }
}
```

Important compatibility note:

- Sing-box docs say official Hysteria2 `userpass` is not provided as an alias. If the panel subscription uses official `userpass`, the actual Sing-box password must be `<username>:<password>`.
- Current SSP mapping is assumed to use `uuid` as the Hy2 password. This is `待验证` with a real subscription fixture before development.

## 8. Generated core config contracts

### 8.1 Xray common config

The Xray renderer always emits:

- log config;
- API inbound on `127.0.0.1:{api_port}`;
- `api.services = ["StatsService", "HandlerService"]`;
- `stats = {}`;
- `policy.levels["0"].statsUserUplink = true`;
- `policy.levels["0"].statsUserDownlink = true`;
- `policy.levels["0"].statsUserOnline = true`;
- one VLESS Reality inbound per SSPanel node assigned to the Xray core;
- direct outbound and block outbound.

Validation:

```text
xray run -test -config config.next.json
```

### 8.2 Sing-box common config

The Sing-box renderer always emits:

- log config;
- `experimental.v2ray_api.listen = "127.0.0.1:{api_port}"`;
- `experimental.v2ray_api.stats.enabled = true`;
- `experimental.v2ray_api.stats.inbounds[]` contains all generated inbound tags;
- `experimental.v2ray_api.stats.users[]` contains all user UUIDs used by AnyTLS/Hysteria2;
- one inbound per SSPanel AnyTLS or Hysteria2 node;
- direct outbound and block outbound.

Validation:

```text
sing-box check -c config.next.json
```

无证据表明 Sing-box V2Ray API supports external dynamic add/delete users. Therefore full render plus process restart is the baseline.

## 9. Protocol capability matrix

| Capability | VLESS Reality / Xray | AnyTLS / Sing-box | Hysteria2 / Sing-box |
| --- | --- | --- | --- |
| Subscription no re-import | full if fields match current panel | full if password remains uuid | full if password remains uuid; `userpass` fixture is 待验证 |
| User authentication | full: VLESS UUID | full: user password from UUID | full: user password from UUID, subject to userpass note |
| User traffic stats | full | full on v1.13.14 source: `metadata.User` + V2Ray API stats | full on v1.13.14 source: `metadata.User` + V2Ray API stats |
| Resettable stats API | full | full | full |
| Accurate `user_id -> IP` | full via Xray online map | unsupported; 无证据表明 official API exposes user IP | unsupported; 无证据表明 official API exposes user IP |
| Realtime device limit | degraded; poll Xray online map then disable/remove user | unsupported | unsupported |
| User-level speed limit | unsupported; 无证据表明 Xray policy supports per-user bandwidth throttle | unsupported; 无证据表明 AnyTLS supports per-user speed | unsupported; 无证据表明 Hysteria2 supports per-user speed; Hy2 has node-level `up_mbps/down_mbps` |
| Node-level speed limit | degraded via OS tc/nft or Core-specific node config where available | degraded via OS tc/nft | full for Hy2 node bandwidth fields |
| Dynamic add/delete users without restart | full for Xray HandlerService, optional phase 2 | unsupported; 无证据表明 external API exists | unsupported; 无证据表明 external API exists |
| Full config restart | full | full | full |
| Hot reload | degraded/optional through Xray API | unsupported | unsupported |
| Last-good rollback | full | full | full |

## 10. Protocol details

### 10.1 VLESS Reality

1. Core: official Xray binary.
2. Subscription fields:
   - Panel continues generating VLESS Reality subscription.
   - Control plane must not change client-side fields: UUID, host, port, flow, security, SNI/serverName, public key, short id, fingerprint, spider/path if panel emits them.
   - Server renderer consumes private-side fields from node `server`: `privateKey`, `dest`, `serverName`, `shortId`, `flow`, `port`.
3. User authentication:
   - Xray `clients[].id = user.uuid`.
   - Xray `clients[].email = "{nodeTag}|{user.uuid}"` for stats identity.
4. User traffic stats:
   - `full`.
   - Evidence: Xray StatsService `GetStats` and `QueryStats` support `reset`; dispatcher creates user traffic counters when policy user stats are enabled.
5. Accurate `user_id -> IP`:
   - `full`.
   - Evidence: Xray `statsUserOnline` and `GetStatsOnlineIpList` map user email to IPs. Control plane maps email suffix UUID back to SSPanel user ID.
6. Realtime device limit:
   - `degraded`.
   - 无证据表明 Xray has built-in accept-time device limit. Control plane can poll online IP count and then remove/disable user through Xray HandlerService or rendered config. This is not instantaneous.
7. User-level speed limit:
   - `unsupported`.
   - 无证据表明 Xray official policy supports per-user bandwidth throttling. Xray policy supports stats and timeouts, not bandwidth rate limiting.
8. Downgrade behavior:
   - Device limit: monitor and delayed disable, with configurable polling interval.
   - Speed limit: log unsupported; optional node-level OS shaping only.
9. Versions:
   - Minimum for this project: `v1.260327.0` because this branch has already validated Reality CCS-related upgrade work locally.
   - Latest release observed on 2026-07-07: Xray `v26.6.27`; treat as `待验证` before production.
   - Verified version recommendation: start with `v1.260327.0`, then test latest binary through acceptance matrix.

### 10.2 AnyTLS

1. Core: official Sing-box binary.
2. Subscription fields:
   - Panel continues generating AnyTLS subscription.
   - Control plane consumes `host`, `port`, `server_name`, and `padding_scheme`.
   - `padding_scheme` must be URL-decoded and parsed as JSON string array.
3. User authentication:
   - Sing-box `users[].name = user.uuid`.
   - Sing-box `users[].password = user.uuid`.
   - Evidence: AnyTLS official docs define `users[].name` and `users[].password`; v1.13.14 source sets `metadata.User` from authenticated user context.
4. User traffic stats:
   - `full`.
   - Evidence: Sing-box v1.13.14 V2Ray API StatsService counts `metadata.User` if it is listed in `stats.users`.
5. Accurate `user_id -> IP`:
   - `unsupported`.
   - 无证据表明 Sing-box official V2Ray API exposes active AnyTLS user IP mappings.
6. Realtime device limit:
   - `unsupported`.
   - It depends on accurate user-to-IP and accept-time enforcement, neither is exposed by official API.
7. User-level speed limit:
   - `unsupported`.
   - 无证据表明 AnyTLS inbound has per-user bandwidth throttle.
8. Downgrade behavior:
   - Traffic accounting remains full.
   - Online IP report disabled by default.
   - Device count and user speed limit reported as unsupported capability.
9. Versions:
   - Minimum: Sing-box `1.12.0` because official docs mark AnyTLS since `1.12.0`.
   - Verified recommendation: Sing-box `1.13.14`.
   - Later versions require config check and live traffic acceptance tests.

### 10.3 Hysteria2

1. Core: official Sing-box binary.
2. Subscription fields:
   - Panel continues generating Hysteria2 subscription.
   - Control plane consumes `host`, `port`, `up_mbps`, `down_mbps`, `obfs`, `obfs_password`, `ignore_client_bandwidth`.
   - `allow_insecure` is client-side subscription behavior and is not a server auth field.
3. User authentication:
   - Default SSP contract: `users[].name = user.uuid`, `users[].password = user.uuid`.
   - `待验证`: if panel emits official Hysteria2 `userpass`, Sing-box requires `<username>:<password>` as the actual password.
4. User traffic stats:
   - `full` after the password mapping fixture is verified.
   - Evidence: Sing-box v1.13.14 Hysteria2 source maps authenticated user ID to `metadata.User`; V2Ray API StatsService counts `metadata.User`.
5. Accurate `user_id -> IP`:
   - `unsupported`.
   - 无证据表明 Sing-box official API exposes active Hysteria2 user IP mappings.
6. Realtime device limit:
   - `unsupported`.
   - No official user IP API or accept-time device limit.
7. User-level speed limit:
   - `unsupported`.
   - Hysteria2 `up_mbps` and `down_mbps` are inbound/node-level fields, not per-user fields.
8. Downgrade behavior:
   - Node-level bandwidth: `full` via Hy2 `up_mbps/down_mbps`.
   - User speed limit: unsupported.
   - Device limit and online IP: disabled by default.
9. Versions:
   - Project minimum: Sing-box `>= 1.12.0` to share the same core as AnyTLS.
   - Hysteria2 exact introduction version in Sing-box is `待验证`; docs show option changes in `1.11.0`.
   - Verified recommendation: Sing-box `1.13.14`.

## 11. Traffic accounting design

### 11.1 Why not reset by default

Both Xray and Sing-box support resettable counters. However, defaulting to reset after panel report is risky:

```text
read counter -> POST panel -> reset core -> crash before local commit
```

or:

```text
read+reset core -> POST panel timeout, actual panel may have committed
```

Because SSPanel `mod_mu` traffic API has no known idempotency key, exactly-once delivery cannot be proven for unknown HTTP outcomes. Therefore the default mode is snapshot-plus-journal, not reset.

### 11.2 Ledger model

Persistent state in `runtime/state.db`:

```text
CoreEpoch
  core_name
  process_start_time
  binary_version
  config_hash

TrafficCursor
  core_name
  node_id
  node_tag
  user_id
  user_key
  uplink_observed
  downlink_observed
  unreported_uplink
  unreported_downlink
  updated_at

ReportBatch
  report_id
  node_id
  status: pending | acknowledged | unknown | quarantined
  payload_hash
  created_at
  acked_at
```

### 11.3 Poll state machine

```text
POLL_START
  -> query official stats reset=false
  -> for each user counter:
       if counter >= last_observed:
         delta = counter - last_observed
       else:
         core restart detected; delta = counter
       unreported += delta
       last_observed = counter
  -> fsync state
  -> POLL_DONE
```

### 11.4 Report state machine

```text
BUILD_BATCH
  -> select users with unreported > 0
  -> write ReportBatch(status=pending)
  -> POST /mod_mu/users/traffic
  -> if definite success:
       subtract batch deltas from unreported
       mark acknowledged
  -> if definite panel rejection:
       keep unreported
       mark pending with backoff
  -> if timeout or connection reset after send:
       mark unknown
       do not retry automatically
       alert operator
```

Rationale:

- Definite success avoids undercount.
- Definite failure avoids losing traffic.
- Unknown outcome cannot be solved without panel idempotency support. Retrying can double count; discarding can undercount. The safe default is quarantine and alert.

Optional panel enhancement:

- Add a backward-compatible `report_id` accepted by your SSP panel.
- If panel stores `report_id` per node, unknown outcomes become retryable and traffic reporting becomes `full` even under network ambiguity.

### 11.5 Restart correctness

Controlled core restart:

```text
PRESTOP_STATS_FLUSH
  -> query all counters reset=false
  -> persist unreported deltas
  -> stop old process
  -> start new process
  -> set new CoreEpoch baseline to zero
```

This is `full` for controlled restarts.

Unexpected core crash:

- Official Xray and Sing-box counters are in-memory; 无证据表明 official binaries persist traffic counters across unexpected process crash.
- The control plane can reduce loss by polling and fsyncing frequently.
- Traffic between the last successful poll and the crash is `degraded` and may be lost.
- No double counting is still maintained by the ledger.

## 12. Panel failure and user deletion policy

### 12.1 Node sync failures

If node info request fails due HTTP error, timeout, invalid JSON, or `ret != 1`:

- keep last known desired node config;
- do not remove node;
- do not remove users;
- continue running last-good core config;
- mark panel state `stale`.

### 12.2 User sync failures

If user request fails due HTTP error, timeout, invalid JSON, or `ret != 1`:

- keep previous user list;
- do not delete users;
- continue traffic reporting for known users;
- log `sync_users=failed`.

### 12.3 HTTP 304

HTTP 304 means no change:

- keep current desired state;
- do not render or restart;
- continue traffic polling.

### 12.4 Empty user list

A successful response with `ret=1` and `data=[]` is ambiguous.

Default policy:

- First empty response: do not delete users; mark `empty_seen=1`.
- Consecutive empty responses reaching `EmptyUserConfirmations` default `2`: apply empty user list.
- Before applying empty list, flush traffic for all current users.
- Config option `AllowImmediateEmptyUserDelete=false` by default.

Reason:

- A real all-user deletion should eventually apply.
- A transient panel bug must not drop all users immediately.

### 12.5 Deleting individual missing users

Delete a user only when:

- the latest user sync is successful;
- response is non-empty, or empty is confirmed by policy;
- user was present before and absent now;
- pre-delete traffic flush for that user has succeeded or is persisted in ledger.

For Xray:

- Phase 1: render full config and restart.
- Phase 2 optional: use HandlerService `AlterInbound(RemoveUserOperation)` after traffic flush.

For Sing-box:

- render full config and restart.
- 无证据表明 official Sing-box binary exposes external dynamic user deletion for AnyTLS/Hysteria2.

## 13. Apply, health check, and rollback FSM

### 13.1 Apply states

```text
IDLE
  -> RENDER_NEXT
  -> VALIDATE_NEXT
  -> PRESTOP_STATS_FLUSH
  -> INSTALL_NEXT
  -> START_OR_RESTART
  -> HEALTH_CHECK
  -> COMMIT
  -> IDLE
```

Failure path:

```text
VALIDATE_NEXT failed
  -> discard config.next.json
  -> keep old process
  -> IDLE

START_OR_RESTART or HEALTH_CHECK failed
  -> RESTORE_LAST_GOOD
  -> START_LAST_GOOD
  -> HEALTH_CHECK_LAST_GOOD
  -> if success: ROLLBACK_COMMIT
  -> if failed: FATAL_CORE_DOWN
```

### 13.2 Render and validate

Steps:

1. Render deterministic JSON to `config.next.json.tmp`.
2. fsync file and parent directory.
3. atomic rename to `config.next.json`.
4. Run official validator:
   - Xray: `xray run -test -config config.next.json`.
   - Sing-box: `sing-box check -c config.next.json`.
5. If validator fails, do not stop current process.

### 13.3 Install next config

Before stopping old process:

- flush stats snapshot;
- persist ledger;
- copy current `config.json` to `last-good.json` if current health is good;
- atomic rename `config.next.json` to `config.json`.

### 13.4 Health checks

Required:

- process is running;
- API bind is reachable on loopback;
- stats API responds;
- expected listen sockets exist;
- rendered config hash matches running process metadata recorded by control plane.

Protocol-specific:

- Xray: `StatsService.GetSysStats` or equivalent API check.
- Sing-box: V2Ray API `GetSysStats` or stats query check.

Optional synthetic tests:

- VLESS Reality handshake fixture on staging.
- AnyTLS client probe on staging.
- Hysteria2 client probe on staging.

### 13.5 Rollback

Rollback must:

- restore `last-good.json`;
- start same binary version that last-good used, if binary upgrade was part of the apply;
- pass health checks;
- keep traffic ledger from pre-stop flush;
- log config hash and reason.

If rollback fails:

- do not delete state;
- mark `FATAL_CORE_DOWN`;
- keep trying last-good with exponential backoff;
- alert operator.

## 14. Core binary install, upgrade, and rollback

### 14.1 Install source

Installer downloads official release artifacts:

- Xray from XTLS GitHub releases.
- Sing-box from SagerNet GitHub releases.

No Core binary is vendored into the control-plane Go module.

### 14.2 Checksum policy

Preferred:

- verify official checksum file if the project publishes one;
- verify archive hash against the checksum.

Fallback:

- installer manifest pins SHA256 for the exact artifact URL;
- if no checksum is available, require explicit `--allow-unverified-download` and refuse by default.

### 14.3 Version detection

On startup:

```text
xray version
sing-box version
```

The detected version is recorded in state and logs. Startup fails if below `MinVersion`, unless `AllowUnsupportedCoreVersion=true`.

### 14.4 Upgrade flow

```text
download artifact to cache
  -> verify checksum
  -> extract binary to versioned path
  -> run binary version
  -> validate current config with new binary
  -> switch symlink atomically
  -> controlled restart
  -> health check
  -> commit or rollback symlink
```

Binary layout:

```text
/usr/local/lib/v2bx-control/cores/
  xray/v1.260327.0/xray
  xray/v26.6.27/xray
  sing-box/1.13.14/sing-box
/usr/local/bin/xray -> /usr/local/lib/v2bx-control/cores/xray/v1.260327.0/xray
/usr/local/bin/sing-box -> /usr/local/lib/v2bx-control/cores/sing-box/1.13.14/sing-box
```

Automatic upgrade is disabled by default. Operators trigger upgrade explicitly.

## 15. Online IP and identity boundary

### 15.1 Xray

Xray identity boundary:

- authenticated VLESS user attaches `user.Email`;
- dispatcher records source IP under `user>>>{email}>>>online` when `statsUserOnline=true`;
- StatsService exposes online IP list for that name.

Therefore VLESS Reality on Xray supports accurate `user_id -> IP` as `full`, assuming:

- email uses deterministic `{nodeTag}|{uuid}`;
- UUID-to-user ID mapping is current;
- API is reachable.

### 15.2 Sing-box

Sing-box identity boundary:

- v1.13.14 source confirms AnyTLS and Hysteria2 put authenticated name into `metadata.User`.
- V2Ray API StatsService uses `metadata.User` for traffic counters.
- 无证据表明 official Sing-box V2Ray API exposes active connection list, online IP list, or user-to-IP map.

Therefore:

- traffic stats are `full`;
- online IP is `unsupported`;
- device limit based on online IP is `unsupported`.

### 15.3 OS socket fallback

OS socket tables can map:

```text
remote IP -> local port -> node tag
```

They cannot map:

```text
remote IP -> authenticated user
```

unless the core exposes user identity. For Sing-box AnyTLS/Hysteria2, OS socket fallback is node-level only and must not be reported as user-level online IP.

### 15.4 Log parsing

Sing-box logs can include user names in connection logs. However:

- docs do not define log format as stable API;
- logs do not guarantee lifecycle or exact disconnect semantics;
- parsing logs for enforcement is not reliable enough.

Log-derived user IP is `degraded`, opt-in only, and must not be used for hard device limit enforcement.

## 16. Limits and rules

### 16.1 Device limit

Required behavior:

- Preserve panel fields and visibility.
- Enforce only when official evidence supports user identity and a control action.

VLESS Reality:

- Online IP count is available through Xray.
- Enforcement is delayed: poll online IP, compare to `node_connector`, then remove user or render disabled config.
- Level: `degraded`.

AnyTLS/Hysteria2:

- Accurate user IP is unavailable.
- Level: `unsupported`.

### 16.2 User speed limit

No protocol in this rewrite has verified official per-user bandwidth throttling:

- Xray: 无证据表明 policy supports per-user bandwidth rate limiting.
- AnyTLS: 无证据表明 official inbound supports per-user bandwidth.
- Hysteria2: official fields are node-level, not user-level.

Default level: `unsupported`.

Optional OS shaping:

- Node/port-level shaping may be implemented with `tc`/nftables.
- This is `degraded` and not equivalent to SSPanel per-user speed limit.

### 16.3 Domain and protocol rules

Panel block rules can be rendered where official routing supports them:

- Xray: route to blackhole outbound.
- Sing-box: route rule with block action.

This is separate from traffic stats and device limits.

## 17. Hot update strategy

Baseline:

- full config render;
- config check;
- controlled restart;
- rollback on failure.

Xray optional phase 2:

- HandlerService source confirms `AddInbound`, `RemoveInbound`, `AlterInbound`, `AddUserOperation`, and `RemoveUserOperation`.
- Use only after full-render path is stable.
- Even when using hot add/remove, periodically reconcile with full rendered config.

Sing-box:

- 无证据表明 official V2Ray API exposes external AddUser/RemoveUser for AnyTLS/Hysteria2.
- Use full config restart.

## 18. Security and file permissions

### 18.1 Process user

Recommended:

- create system user `v2bx`;
- run control plane as `v2bx`;
- run external cores as `v2bx`;
- if binding ports below 1024, use `CAP_NET_BIND_SERVICE` or systemd ambient capability instead of root.

### 18.2 File permissions

```text
/etc/v2bx-control                 0750 root:v2bx
/etc/v2bx-control/config.json      0640 root:v2bx
/etc/v2bx-control/cert             0750 root:v2bx
private keys                       0640 root:v2bx
runtime directories                0750 v2bx:v2bx
state.db                           0600 v2bx:v2bx
logs                               0640 v2bx:v2bx
core binaries                      0755 root:root
```

### 18.3 API binding

- Xray API and Sing-box V2Ray API bind to `127.0.0.1` or Unix socket only.
- Never expose Core API ports publicly.
- Firewall install script must deny external access to API ports.

### 18.4 Command execution

- Execute binaries with argv arrays, not shell strings.
- Validate binary path owner and mode.
- Reject writable-by-world binary paths.
- Do not interpolate panel fields into shell commands.

### 18.5 Secrets

- API key must not appear in generated Core config.
- Logs must redact panel API key, Reality private key, TLS key paths if needed, and user UUIDs at info level.

## 19. Acceptance test matrix

### 19.1 Static tests

| Test | Expected |
| --- | --- |
| `go.mod` has no Xray/Sing/Hysteria modules | pass |
| SSPanel node fixture sort 16 renders deterministic Xray JSON | pass |
| SSPanel node fixture sort 18 renders deterministic Sing JSON | pass |
| SSPanel node fixture sort 17 renders deterministic Sing JSON | pass |
| Unknown `CoreName` fails startup | pass |
| Ambiguous old `Core` value fails startup | pass |
| Missing Reality private key fails render | pass |
| Hy2 `obfs=salamander` without password fails render | pass |
| Empty `obfs=plain` omits Sing Hy2 obfs | pass |

### 19.2 Config validation tests

| Test | Command | Expected |
| --- | --- | --- |
| Xray Reality config | `xray run -test -config config.json` | pass |
| Sing AnyTLS config | `sing-box check -c config.json` | pass |
| Sing Hysteria2 config | `sing-box check -c config.json` | pass |
| Bad config rollback | inject invalid config | old process continues or last-good starts |

### 19.3 Integration tests

| Test | Expected |
| --- | --- |
| VLESS Reality client connects without subscription change | pass |
| AnyTLS client connects without subscription change | pass |
| Hysteria2 client connects without subscription change | pass |
| Xray user traffic appears under `{nodeTag}|uuid` | pass |
| Sing AnyTLS traffic appears under `uuid` | pass |
| Sing Hy2 traffic appears under `uuid` | pass |
| Controlled Xray restart flushes traffic before stop | no loss/no duplicate |
| Controlled Sing restart flushes traffic before stop | no loss/no duplicate |
| V2BX-control restart preserves ledger | no duplicate |
| Panel 500 during user sync keeps users | no deletion |
| Panel timeout during traffic report keeps pending batch | no data loss |
| HTTP unknown outcome quarantines batch | no automatic double count |
| First empty user list does not delete users | pass |
| Confirmed empty user list deletes after pre-flush | pass |
| Xray online IP report maps to user_id | pass |
| Sing online IP is not reported by default | pass |
| Xray over device limit disables user after polling | degraded behavior documented |
| AnyTLS/Hy2 device limit logs unsupported | pass |
| Binary upgrade validates config before restart | pass |
| Binary upgrade failure rolls back symlink and config | pass |

### 19.4 Production staging tests

Minimum staging nodes:

- one VLESS Reality node matching existing node 62 format;
- one AnyTLS node;
- one Hysteria2 node.

Each staging test must record:

- exact panel node `server` field;
- one generated subscription link with sensitive values redacted;
- generated Core JSON;
- binary version;
- config check output;
- traffic report before/after values;
- rollback test result.

## 20. Development roadmap

### Phase 0: Design lock and fixtures

Deliverables:

- this document accepted;
- real SSPanel fixtures for sort 16, 17, 18;
- redacted subscription links for VLESS Reality, AnyTLS, Hysteria2;
- expected generated Xray/Sing JSON golden files;
- binary versions selected.

Exit criteria:

- no `待验证` item blocks protocol mapping.

### Phase 1: Render-only control plane

Deliverables:

- new repo skeleton;
- config loader;
- SSPanel DTO parser;
- desired-state model;
- Xray renderer for VLESS Reality;
- Sing renderer for AnyTLS and Hysteria2;
- golden tests.

Exit criteria:

- no embedded Core modules in `go.mod`;
- generated JSON passes official config validators.

### Phase 2: Process manager and rollback

Deliverables:

- process supervisor;
- config check;
- controlled restart;
- health checks;
- last-good rollback;
- binary version detection.

Exit criteria:

- invalid config never replaces running last-good.

### Phase 3: Stats ledger and SSPanel reporting

Deliverables:

- Xray StatsService client;
- Sing V2Ray API StatsService client;
- snapshot ledger;
- report journal;
- controlled restart pre-stop flush;
- panel failure handling.

Exit criteria:

- controlled restart has no traffic loss or duplicate in staging.

### Phase 4: Online IP and limits

Deliverables:

- Xray online IP collector;
- SSPanel alive IP reporter;
- capability logging for unsupported Sing online IP;
- degraded Xray device limit policy.

Exit criteria:

- Xray online IP reports correctly;
- Sing unsupported behavior is explicit and does not fake user IP.

### Phase 5: Installer and binary upgrade

Deliverables:

- install script;
- systemd unit;
- checksum verification;
- versioned core binary cache;
- binary rollback.

Exit criteria:

- fresh VPS install is one command;
- replacing official Core binary does not recompile control plane.

## 21. Development prerequisites by priority

P0:

1. Export real SSPanel node JSON for sort 16, 17, 18.
2. Export one redacted subscription link for VLESS Reality, AnyTLS, Hysteria2.
3. Confirm Hysteria2 password format: plain UUID vs official `userpass`.
4. Confirm Hysteria2 obfs field format currently emitted by your panel.
5. Decide initial binary versions: Xray `v1.260327.0` or newer; Sing-box `1.13.14`.
6. Decide whether your SSP panel can accept optional `report_id` for traffic idempotency.

P1:

7. Choose new repository name and license.
8. Write golden JSON fixtures and run official config validators.
9. Define exact `CoreName` config compatibility behavior.
10. Define operational policy for first empty user list and confirmed empty deletion.

P2:

11. Decide whether to implement Xray HandlerService hot user updates in phase 2 or keep full restart only.
12. Decide whether node-level OS shaping is worth implementing for speed-limit downgrade.
13. Decide log redaction level for UUIDs and private keys.
14. Decide installer checksum source and release mirror strategy.

## 22. Final architecture decision

Adopt a synchronous architecture rewrite into a new control-plane project.

Keep:

- SSPanel mod_mu API contract;
- current panel field semantics;
- user and node polling concepts;
- node status reporting;
- certificate management concepts if copied with license compliance.

Replace:

- embedded Xray layer with Xray JSON renderer, process manager, StatsService client, optional HandlerService client;
- embedded Sing-box layer with Sing JSON renderer, process manager, V2Ray API StatsService client;
- embedded Hysteria2 layer with Sing-box Hysteria2 inbound renderer;
- in-process hooks with official APIs and explicit degraded/unsupported capability flags.

Do not implement:

- fake user online IP for Sing-box AnyTLS/Hysteria2;
- fake realtime device limit where user IP is unavailable;
- per-user speed limiting without official Core support;
- Core embedding of any kind.
