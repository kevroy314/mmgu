# Hosting Hallkeeper

Hallkeeper is one container (web app + Discord bot + scheduler) plus optional helpers. It needs very
little: 1 CPU, 1 GB RAM, a few GB of disk. The only heavy part is the optional local AI that reads
screenshots, which wants an NVIDIA GPU with 8 GB or more.

| Setup | Good for | Cost | Screenshot reading |
|---|---|---|---|
| [Home PC + Docker](#home-pc-with-docker) | A guild member with an always-on PC | Free | Local GPU (Ollama) |
| [Home PC + Tailscale Funnel](#sharing-with-tailscale-funnel) | Same, no domain or router changes | Free | Local GPU |
| [Google Cloud VM](#google-cloud-vm) | Nobody wants to run Docker at home | ≈ $15/month | Claude Haiku (pennies) or a home GPU over Tailscale |
| [Cloud Run + Cloud SQL](#cloud-run-and-cloud-sql) | Larger communities, managed everything | ≈ $30–45/month | Claude Haiku |

The Discord bot only makes **outgoing** connections, so Discord commands work from any of these without
opening ports. Only the web hall needs to be reachable by members.

## Home PC with Docker

```bash
git clone https://github.com/kevroy314/mmgu.git && cd mmgu
cp .env.example .env            # fill in guild name, secret key, Discord settings (docs/discord.md)
docker compose up -d            # the hall at http://localhost:8420
docker compose --profile ai up -d                  # optional: local AI (NVIDIA GPU + container toolkit)
docker compose --profile ai-setup run --rm ollama-pull   # once: download the vision model (~6 GB)
```

Updating: `git pull && docker compose up -d --build`. Database migrations run automatically on start.

Your data lives in `./data` (the SQLite database and uploaded screenshots). Back it up, or use the
`backup` profile ([Litestream](https://litestream.io)) to stream it continuously to S3, Backblaze B2,
Cloudflare R2 or Google Cloud Storage.

## Sharing with Tailscale Funnel

Funnel gives the hall a public `https://hallkeeper.<your-tailnet>.ts.net` address without a domain,
router changes or open ports. Guildmates don't install anything. Funnel is free on Tailscale's
Personal plan (it's labelled beta and has bandwidth limits that don't matter for a guild).

1. In the Tailscale admin console, enable HTTPS and Funnel for your tailnet, and create an auth key.
2. Put it in `.env` as `TS_AUTHKEY=...`; set `MMGU_BASE_URL=https://hallkeeper.<tailnet>.ts.net`.
3. `docker compose --profile share up -d`

Because the address is public, keep `MMGU_AUTH_MODES=discord` so only people in your Discord server get in.
(Plain Tailscale *Serve* instead of Funnel would require every member to join your tailnet, and the free
plan caps that at a handful of users.)

### Cloudflare Tunnel

If someone owns a domain on Cloudflare, a tunnel gives a nicer address (`guild.example.com`). Add a
`cloudflared` service with `TUNNEL_TOKEN` from the Cloudflare dashboard and point the tunnel at
`http://hallkeeper:8420`. Cloudflare Access (free up to 50 users) can sit in front; use `header` auth
mode with it (see `deploy/home/nginx-example.conf` for the header contract).

### Behind your own reverse proxy

`deploy/home/nginx-example.conf` shows nginx + oauth2-proxy. The hall trusts the proxy's email header
only when the request also carries `X-MMGU-Proxy-Secret`, so nothing else on the network can pose as a
member. Stream overlays (`/overlay/...`) must bypass the sign-in gateway: OBS can't log in, and each
overlay URL carries its own secret token.

## Google Cloud VM

The simplest cloud option runs exactly the same compose file on a small VM.

```bash
PROJECT=my-gcp-project ./deploy/gcp/create-vm.sh          # e2-small, Debian 12, Docker
gcloud compute scp .env hallkeeper:/opt/hallkeeper/.env --zone us-central1-a
gcloud compute ssh hallkeeper --zone us-central1-a -- 'cd /opt/hallkeeper && sudo docker compose up -d'
```

Then expose it with a Cloudflare Tunnel (no open ports, recommended) or Caddy (open 80/443 in the VPC
firewall for the `hallkeeper` network tag, and run Caddy with `reverse_proxy localhost:8420`).

<a id="caddy"></a>Caddyfile for the Caddy route:

```
guild.example.com {
    reverse_proxy localhost:8420
}
```

**Screenshot reading on a VM without a GPU:** in the Steward's Office, set the Screenshot Reader add-on's
provider to `anthropic` and add an API key (Claude Haiku costs a fraction of a cent per screenshot).
Or keep the GPU at home: join the VM and the home PC to the same tailnet and set
`MMGU_OLLAMA_URL=http://<home-pc>.<tailnet>.ts.net:11434`.

**Moving from home to the VM:** stop the hall at home, copy `data/` and `.env` to `/opt/hallkeeper/`
on the VM (or restore from Litestream), update `MMGU_BASE_URL` and the Discord OAuth redirect URL, start
it. Nothing else changes.

**Backups:** create a GCS bucket and set `LITESTREAM_REPLICA_URL=gcs://<bucket>/hallkeeper`, then
`docker compose --profile backup up -d`. Give the VM's service account `Storage Object Admin` on the bucket.

## Cloud Run and Cloud SQL

For a managed setup with no VM to patch:

- **Cloud Run** service from the same image, `--min-instances=1 --no-cpu-throttling` (the Discord bot
  and scheduler need an always-on process), 1 vCPU / 1 GiB, max instances 1 (the bot must be a single
  process).
- **Cloud SQL for PostgreSQL** (smallest shared-core tier): set
  `MMGU_DATABASE_URL=postgresql+asyncpg://...` and install the `postgres` extra (the image already does).
  Search falls back from SQLite full-text search to simple substring matching on Postgres.
- **Uploads**: mount a Cloud Storage bucket as a volume at `/data/uploads` (Cloud Run volume mounts).
- **Secrets**: Secret Manager for `MMGU_SECRET_KEY`, Discord token and client secret.

This costs more than the VM (the always-on instance dominates) and has more moving parts. Start with the
VM unless you have a reason not to.
