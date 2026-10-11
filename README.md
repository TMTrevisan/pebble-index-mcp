# muse — Pebble Index 01 MCP bridge

Lets Todd talk to Muse through his Pebble Index 01 smart ring. Voice goes
ring → Pebble app → Pebble cloud agent → this server's tools, and the answer
comes back as a phone notification.

Three tools:

| Tool | What it does |
|---|---|
| `ha` | Home Assistant over the local REST API — lights, switches, fans, climate (including "set the thermostat to 72"), covers, media players, sensor reads. Never touches locks or alarms. |
| `wyze` | On/off for exactly two lights: "Todd's light" (bedroom) and "Lamp" (living room). Nothing else on the Wyze account. |
| `ask` | Anything else — forwarded to Home Assistant's own conversation agent, so whatever assistant Todd has configured in HA answers. |

## Run it (on the Home Assistant box)

This is built for Todd's Home Assistant OS machine, deployed through the
**Portainer add-on** (already installed) → **Stacks** → **Add stack**,
paste the contents of `docker-compose.yml`, and add the environment
variables below. Or on any Docker host on the home network:

```bash
cp .env.example .env && chmod 600 .env
# fill in .env (see below), then:
docker compose up -d
```

The server listens on `:8765` and probes for Home Assistant at
`http://homeassistant:8123` (then `localhost:8123`, then
`host.docker.internal:8123`). Set `HASS_URL` explicitly if needed.

### 1. Home Assistant token

HA → click your profile (bottom-left) → **Long-Lived Access Tokens** →
Create Token. Paste it as `HASS_TOKEN` in `.env`. The server talks to HA
over the local network — no cloud round-trip.

### 2. Wyze credentials (for the `wyze` tool)

`WYZE_EMAIL`, `WYZE_PASSWORD`, `WYZE_KEY_ID`, `WYZE_API_KEY` in `.env`.
Without them, `wyze` says it isn't set up; everything else works.

### 4. Bearer token (Pebble app → server auth)

Generate one and put it in `.env`:

```bash
openssl rand -hex 32
```

Every request without `Authorization: Bearer <token>` gets a 401.

### 5. Expose it through the existing DuckDNS + HTTPS setup

The Pebble app needs a public `https://` URL ending in `/mcp`. Two options:

**A. NGINX Home Assistant SSL proxy add-on (recommended — already installed).**
Add a location to the existing proxy config:

```nginx
location /mcp {
    proxy_pass http://<container-ip-or-hostname>:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
}
```

Then the MCP URL is `https://<your-duckdns-host>/mcp`.

**B. New port forward.** Forward an external port to the box's 8765 and
terminate TLS in front of it (the app requires HTTPS).

Also add your public hostname to `MCP_ALLOWED_HOSTS` in `.env`
(e.g. `MCP_ALLOWED_HOSTS=petitcasa.duckdns.org`) — the server rejects
requests whose Host header isn't allowlisted (DNS-rebinding protection).

## Phone setup (Pebble app)

1. Index tab → **MCP Settings** → Groups → create a sandbox group.
   - **Model type: `Default`** — the `Index Agent` type ignores custom MCP
     servers. This is the gotcha that bites everyone.
2. **MCP Servers** tab → Add HTTP MCP Server:
   - **Name:** `muse` (no spaces — spaces break tool routing)
   - **URL:** `https://<your-duckdns-host>/mcp`
   - **Type:** **Streamable** (not SSE)
   - **Authorization:** literally `Bearer <your-token>` — the `Bearer `
     prefix is REQUIRED; the raw token alone 401s on every request
   - **Group:** the sandbox group from step 1
3. In the server's settings, select the `ring_persona` prompt so it joins
   the agent's system prompt.
4. Index settings → **"Double click and hold"** → assign the sandbox group.
   Keep single click as the default Index agent for private offline notes.

## How replies get back to the phone

No separate push setup needed. The Pebble app builds the completion
notification from the **last tool result** — every tool returns the
`coreSchema` Response contract, so the phone shows the *answer*, not the
question. (A plain-text tool result would show the transcription instead.)

## Security notes

- Bearer token on every request (constant-time compare); 401 otherwise.
- DNS-rebinding protection: only loopback + `MCP_ALLOWED_HOSTS` are served.
- `.env` is git-ignored; the example ships empty. Secrets never appear in
  logs — the server logs that a token is missing, never its value.
- The `ha` tool refuses anything matching lock/unlock/alarm/disarm.
- The `wyze` tool can only address the two MACs in `WYZE_TODDS_MAC` /
  `WYZE_LAMP_MAC`; similarly-named bulbs are scrubbed before matching.

## For developers

```bash
python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q
```

`mcp==1.29.0` is pinned deliberately: `server.py` registers the
`ring_persona` prompt on the low-level server so `prompts/list` emits
`"arguments": null`, which the Pebble app's client requires (it hides
prompts whose `arguments` is a non-null list). Bump only with
`test_prompts_list_arguments_null` green.
