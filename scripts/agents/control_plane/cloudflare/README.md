# Cloudflare Tunnel + Access templates (ENG-AGENT-02-S6, CPX-06)

Zero-secret templates for exposing the local **Octarel** dashboard
(`127.0.0.1:8877`) at `https://dev.octascene.com` through Cloudflare Tunnel,
gated by Cloudflare Access.

After CPX-06 the process on 8877 is standalone Octarel, not the embedded
Octages daemon. The tunnel and Access application stay the same. Operator
runbook: [`docs/engineering/CUTOVER.md`](../../../../docs/engineering/CUTOVER.md). None of these files contain a real credential,
tunnel token, account ID, or password — every placeholder is written in
`ALL_CAPS_WITH_UNDERSCORES` and must be replaced locally. **Never commit the
filled-in versions of these files.** They are already covered by
`.gitignore` if you save them under their real (non-`.example`) names inside
this directory.

Full walkthrough: [`docs/REMOTE_ACCESS.md`](../../../../docs/REMOTE_ACCESS.md).

| File | Purpose |
| --- | --- |
| `cloudflared-config.example.yml` | `cloudflared` tunnel ingress config: routes `dev.octascene.com` to the local dashboard. |
| `remote-access.env.example` | Environment variables the Control Center reads to enable/configure the *application-level* remote-access defense-in-depth (optional; Cloudflare Access at the edge is the primary boundary either way). |
| `com.octascene.orchestrator-tunnel.plist.example` | macOS `launchd` service so `cloudflared` starts automatically at login, restarts after a crash, and stays running without a terminal window. |
| `com.octascene.orchestrator-dashboard.plist.example` | macOS `launchd` service for the Control Center dashboard itself — same auto-start/auto-restart treatment, still loopback-only. |

Copy each `*.example` file to the same name without `.example`, fill in your
own values, and never `git add` the filled-in copy.
