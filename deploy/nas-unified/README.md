# NAS unified workbench deployment

The workbench is deployed in two phases. Phase 1 keeps the existing company
LAN origin working immediately. Phase 2 changes the shared origin to the
domestic HTTPS domain after DNS and the Tencent Cloud gateway are ready. H5
routes, capability tokens, and task behavior stay unchanged across the cutover.

## Phase 1: company LAN pilot

The employee-facing origin is:

```text
http://192.168.1.35:6185
```

The NAS forwards that LAN listener to the container mapping:

```text
192.168.1.35:6185 -> 127.0.0.1:6285 -> dc-agent:6185
```

Compose binds `6285` only to NAS loopback, so employees use `6185` and the
container port is not separately exposed to the LAN. Do not configure an
internet router port forward for either port.

Set the shared draft-administration token in the untracked Compose environment:

```dotenv
DC_ASSISTANT_H5_ADMIN_TOKEN=<same high-entropy value used by the assistant runtime>
```

`DC_ASSISTANT_H5_ORIGIN` defaults to the LAN origin during this phase. It may be
set explicitly to the same value in the untracked `.env` file.

## Phase 2: domestic domain cutover

After DNS and the Tencent Cloud gateway are ready, change only:

```dotenv
DC_ASSISTANT_H5_ORIGIN=https://workbench.gx-dianchi.cn
```

The Tencent Cloud gateway terminates HTTPS and reaches the NAS through an
outbound reverse tunnel initiated by the NAS. The NAS still requires no public
inbound port. Add the same HTTPS origin to the Feishu web-app security domains
and `FEISHU_OAUTH_RETURN_ORIGINS` before switching the environment variable.

## Validation

From the repository root:

```bash
docker compose -f deploy/nas-unified/compose.yml config
docker compose -f deploy/nas-unified/compose.yml up -d
docker compose -f deploy/nas-unified/compose.yml ps
```

For phase 1, verify:

- `dc-agent-unified` is healthy.
- `http://192.168.1.35:6185/api/chat/health` returns `status=ok` on company LAN.
- `192.168.1.35:6285` is no longer directly reachable from an employee device.
- Feishu web opens every task workspace and its expired-link recovery action.

For phase 2, repeat the same workspace checks with
`https://workbench.gx-dianchi.cn`. Capability paths must never be written to
request logs; the existing H5 audit keeps only timestamp, source IP, method,
status, and service name.
