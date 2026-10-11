# pebble-index-mcp — Pebble Index 01 MCP Bridge

Talk to your own AI assistant through a Pebble Index 01 smart ring. Voice goes
ring → Pebble app → Pebble cloud agent → this server's tools, and the answer
comes back as a phone notification.

Three tools ship out of the box:

| Tool | What it does |
|---|---|
| `ha` | Home Assistant over the local REST API — lights, switches, fans, climate, covers, media players, sensor reads. Never touches locks or alarms. |
| `wyze` | On/off for Wyze lights. **You must configure which lights** (see below) — by default it controls nothing until you set it up. |
| `ask_muse` | Anything else — the question is queued for your assistant, who answers over an HTTPS bridge (`GET /queue/pending` + `POST /queue/answer`). See "How ask_muse works" below. |

## What you need

- A Pebble Index 01 smart ring + the Pebble mobile app
- A Docker host on your home network (Synology NAS, Raspberry Pi, any Linux box)
- Home Assistant (optional — only needed for the `ha` tool)
- A Wyze account with lights (optional — only needed for the `wyze` tool)
- A public HTTPS URL for the server (DuckDNS + reverse proxy, Cloudflare Tunnel, etc.)
- Your AI assistant with the ability to poll an HTTPS endpoint (for `ask_muse`)

## Quick start

### 1. Clone and configure

```bash
git clone -b pi-bridge https://github.com/TMTrevisan/pebble-index-mcp.git
cd pebble-index-mcp
cp .env.example .env && chmod 600 .env
```

Fill in `.env` (see "Environment variables" below).

### 2. Deploy with Docker Compose

```yaml
services:
  muse:
    container_name: muse-pebble-mcp
    image: acisco2/pebble-index-mcp:pi-bridge
    restart: unless-stopped
    ports:
      - 8765:8765
    environment:
      - MCP_BEARER_TOKEN=${MCP_BEARER_TOKEN}
      - HASS_TOKEN=${HASS_TOKEN}
      - HASS_URL=${HASS_URL}
      - WYZE_EMAIL=${WYZE_EMAIL}
      - WYZE_PASSWORD=${WYZE_PASSWORD}
      - WYZE_KEY_ID=${WYZE_KEY_ID}
      - WYZE_API_KEY=${WYZE_API_KEY}
      - MCP_ALLOWED_HOSTS=${MCP_ALLOWED_HOSTS}
```

```bash
docker compose up -d
```

The server listens on `:8765`.

### 3. Environment variables

| Variable | Required | Description |
|---|---|---|
| `MCP_BEARER_TOKEN` | Yes | Auth token for the Pebble app. Generate with `openssl rand -hex 32`. |
| `MCP_ALLOWED_HOSTS` | Yes | Your public hostname (e.g. `myhome.duckdns.org`). DNS-rebinding protection. |
| `HASS_URL` | For `ha` | Home Assistant URL (e.g. `http://192.168.1.100:8123`). |
| `HASS_TOKEN` | For `ha` | HA long-lived access token (Profile → Long-Lived Access Tokens). |
| `WYZE_EMAIL` | For `wyze` | Wyze account email. |
| `WYZE_PASSWORD` | For `wyze` | Wyze account password. |
| `WYZE_KEY_ID` | For `wyze` | Wyze API key ID. |
| `WYZE_API_KEY` | For `wyze` | Wyze API key. |

### 4. Expose via HTTPS

The Pebble app requires a public `https://` URL ending in `/mcp`. Add these to your reverse proxy (NGINX, etc.):

```nginx
location /mcp {
    proxy_pass http://<server-ip>:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
}
location /queue/ {
    proxy_pass http://<server-ip>:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
}
```

Your MCP URL will be `https://<your-host>/mcp`.

### 5. Phone setup (Pebble app)

1. Index tab → **MCP Settings** → Groups → create a sandbox group.
   - **Model type: `Default`** — the `Index Agent` type ignores custom MCP servers.
2. **MCP Servers** tab → Add HTTP MCP Server:
   - **Name:** no spaces (e.g. `muse`)
   - **URL:** `https://<your-host>/mcp`
   - **Type:** **Streamable** (not SSE)
   - **Authorization:** literally `Bearer <your-token>` — the `Bearer ` prefix is REQUIRED
   - **Group:** the sandbox group from step 1
3. Select the `ring_persona` prompt for the server.
4. Index settings → **"Double click and hold"** → assign the sandbox group.

## Customizing for your setup

### Wyze lights

The `wyze` tool is hardcoded to two example lights. To use your own:

1. Find your lights' MAC addresses (Wyze app → Device Info).
2. Edit `src/pebble_index_mcp/server.py`:
   - Update `WYZE_TODDS_MAC` and `WYZE_LAMP_MAC` (or rename them)
   - Update the `wyze()` tool description and matching logic
3. Rebuild the Docker image.

### Home Assistant

The `ha` tool works with any Home Assistant instance — just set `HASS_URL` and `HASS_TOKEN`. No code changes needed.

### The `ask_muse` bridge

This tool queues questions for your AI assistant to answer. Your assistant needs to:

1. Poll `GET https://<your-host>/queue/pending` with `Authorization: Bearer <token>` (returns 204 when empty, or JSON with `{"id", "question"}`).
2. POST the answer to `/queue/answer` with `{"id", "answer"}`.
3. The waiting tool picks up the answer (up to 150s timeout) and returns it as a phone notification.

See `bridge/queue.py` for a reference polling client.

## How replies get back to the phone

No push setup needed. The Pebble app builds the completion notification from the **last tool result** — every tool returns the `coreSchema` Response contract, so the phone shows the *answer*, not the question.

## Security notes

- Bearer token on every request (constant-time compare); 401 otherwise.
- DNS-rebinding protection: only loopback + `MCP_ALLOWED_HOSTS` are served.
- `.env` is git-ignored. Secrets never appear in logs.
- The `ha` tool refuses anything matching lock/unlock/alarm/disarm.
- The `wyze` tool only addresses configured MACs.

## For developers

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```
