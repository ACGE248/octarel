# Remote access

Octarel's Control Center binds **loopback only** (`127.0.0.1:8877` by
default). It is not a public HTTP service.

## Recommended topology

```
browser -> https://<your-hostname>
        -> Cloudflare Access  (identity)
        -> Cloudflare Tunnel  (outbound-only)
        -> http://127.0.0.1:8877
        -> Octarel dashboard
```

Templates (zero secrets) live in
`scripts/agents/control_plane/cloudflare/`. Copy the `*.example` files
locally, fill in operator values, and never commit the filled copies.

The historical OctaScene development hostname `dev.octascene.com` is an
operator-specific example of this topology. A public Octarel install should
choose its own hostname and Access application.

## Rules

- Do not bind a non-loopback address.
- Cloudflare Access (or equivalent) remains the public boundary.
- Access audience, team domain, and allow-listed emails stay in the local
  launchd/env, never in git.
- Unauthenticated visitors must hit the identity challenge, not the dashboard.

Operator cutover notes for the original OctaScene hostname:
`docs/engineering/CUTOVER.md`.
