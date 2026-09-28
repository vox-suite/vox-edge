# Vox routing protocol

One public origin (`api.voxagent.in`) is served by this edge. Every path belongs to exactly one namespace.

| Prefix | Served by | Public | Purpose |
|---|---|---|---|
| `/v1/**` | core-api | yes | Versioned client API |
| `/bridge/**` | bridge | yes | Third-party webhooks and media streams (Twilio, WhatsApp) and desktop voice |
| `/internal/v1/**` | core-api, bridge | no | Service-to-service only. The edge never routes it |
| `/health` | edge | yes | Edge liveness |
| `/health/live`, `/health/ready` | core-api, bridge | no | Per-service probes for the platform |

Anything else returns 404.

## Core API (`/v1`)

RPC style. Every endpoint is `POST` with a JSON body, except WebSocket upgrades, which are `GET`.

| Operation | Path |
|---|---|
| create | `POST /v1/<resource>` |
| list | `POST /v1/<resource>/list` |
| get | `POST /v1/<resource>/{id}` |
| update | `POST /v1/<resource>/{id}/update` |
| delete | `POST /v1/<resource>/{id}/delete` |
| domain action | `POST /v1/<resource>/{id}/<verb>` (`cancel`, `approve`, `disable`, `revoke`, `archive`) |
| singleton | `POST /v1/<resource>/get` (`/v1/sms/consent/get`, `/grant`, `/revoke`) |
| socket | `GET /v1/<resource>/{id}/socket` |

Filters and pagination go in the JSON body, never the query string. Send `{}` when there are none.

## Bridge (`/bridge`)

`/bridge/<channel>/<surface>[/<sub>]`, for example `/bridge/twilio/voice`, `/bridge/twilio/voice/stream`, `/bridge/wa`, `/bridge/desktop/voice/session`.

## Internal (`/internal/v1`)

`/internal/v1/<domain>/<action>` on the receiving service, authenticated with the shared service token. Reached over Railway's private network only.

## Required environment

- `CORE_API_UPSTREAM`: for example `vox-core-api.railway.internal:8080`
- `BRIDGE_UPSTREAM`: for example `vox-bridge.railway.internal:8080`
