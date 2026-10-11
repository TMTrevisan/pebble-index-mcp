"""Muse MCP bridge for the Pebble Index 01 smart ring.

Ring -> Pebble app -> Pebble cloud agent -> this server (Streamable HTTP).

Runs on Todd's home network (Home Assistant box via Portainer/Docker),
so the `ha` tool hits Home Assistant's local REST API directly.

Tools:
  ha       - Home Assistant via local REST API (HASS_TOKEN long-lived token).
             Lights, switches, fans, climate (incl. setpoints), covers,
             media players, sensor reads. Never locks or alarms.
  wyze     - On/off for Todd's two authorized Wyze lights ONLY.
  ask_muse - Open-ended questions -> Muse himself, via a file queue bridged
             over HTTPS: GET /queue/pending and POST /queue/answer let an
             off-box agent poll for questions and post answers back.

Every user-facing answer goes through _spoken_response so the Pebble app's
coreSchema contract surfaces the ANSWER (not the question) in the phone
notification.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import (
    CallToolResult,
    GetPromptResult,
    ListPromptsResult,
    PromptMessage,
    TextContent,
)
from mcp.types import Prompt as MCPPrompt
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .homeassistant import HAClient, HAError

logger = logging.getLogger("muse_pebble_mcp")

# ---------------------------------------------------------------- config

TOKEN = os.environ.get("MCP_BEARER_TOKEN", "").strip()

_HASS_URL_ENV = os.environ.get("HASS_URL", "").strip()
_HASS_TOKEN = os.environ.get("HASS_TOKEN", "").strip()

# Wyze direct API (wyze-sdk). Todd fills these in; never committed.
_WYZE = {
    "email": os.environ.get("WYZE_EMAIL", "").strip(),
    "password": os.environ.get("WYZE_PASSWORD", "").strip(),
    "key_id": os.environ.get("WYZE_KEY_ID", "").strip(),
    "api_key": os.environ.get("WYZE_API_KEY", "").strip(),
}
# Standing auth covers exactly these two lights; MACs verified 2026-10-10.
WYZE_TODDS_MAC = os.environ.get("WYZE_TODDS_MAC", "D03F27102015").strip()
WYZE_LAMP_MAC = os.environ.get("WYZE_LAMP_MAC", "2CAA8E560872").strip()
# Other similarly-named bulbs on the account that must NEVER match "Lamp".
_WYZE_DECOYS = (
    "small lamp", "office lamp", "nursery lamp", "logan lamp",
    "kaitlyn's light", "kaitlyn light",
)

# Voice from a ring has no voiceprint auth: never touch locks or alarms.
_HA_DENY_WORDS = ("lock", "unlock", "alarm", "disarm")

# ask_muse bridge: questions land as files in _QUEUE_DIR; an off-box agent
# (Muse's cron) polls GET /queue/pending and posts answers to
# POST /queue/answer. QUEUE_DIR is configurable for the Docker deployment.
_QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", "queue"))
_ASK_TIMEOUT_S = 150
_ASK_POLL_S = 5
_QID_RE = re.compile(r"^[0-9a-f]{32}$")

# DNS-rebinding protection: allow loopback plus any hosts named in
# MCP_ALLOWED_HOSTS (comma-separated). The public DuckDNS hostname goes here.
_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_ALLOWED_HOSTS = _LOOPBACK_HOSTS + [
    h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()
]

mcp = FastMCP(
    "muse",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=_ALLOWED_HOSTS,
    ),
)


def _spoken_response(text: str, question: str | None = None) -> CallToolResult:
    """Wrap a final answer in the Pebble app's coreSchema Response contract.

    The app builds the completion notification from the LAST tool action's
    semantic result; a plain-text result makes it show the transcription
    (the question) instead of the answer. `Response` is the variant whose
    text is surfaced in the notification. `output` is what the cloud agent's
    LLM receives; `content` mirrors it for non-Pebble MCP clients.
    """
    text = " ".join(str(text).split())[:500]  # notification-sized, no newlines
    semantic: dict = {"type": "Response", "text": text}
    if question is not None:
        semantic["question"] = question
    result = CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={"output": text, "semanticResult": semantic},
    )
    result.meta = {"coreSchema": 1}
    return result


# ---------------------------------------------------------------- HA

_HA_URL_CANDIDATES = [
    "http://homeassistant:8123",      # same Docker network as HA container
    "http://localhost:8123",          # host networking
    "http://host.docker.internal:8123",
]
_ha_client: HAClient | None = None


def _ha() -> HAClient:
    """HAClient against the first reachable local HA URL (cached)."""
    global _ha_client
    if _ha_client is not None:
        return _ha_client
    if not _HASS_TOKEN:
        raise HAError("Smart home control unavailable: HASS_TOKEN not configured.")
    candidates = [_HASS_URL_ENV] if _HASS_URL_ENV else _HA_URL_CANDIDATES
    for url in candidates:
        try:
            r = httpx.get(f"{url.rstrip('/')}/api/", timeout=5.0)
            if r.status_code == 200:
                _ha_client = HAClient(url=url, token=_HASS_TOKEN)
                logger.info("Home Assistant reachable at %s", url)
                return _ha_client
        except httpx.HTTPError:
            continue
    raise HAError(
        "Home Assistant isn't reachable locally. Set HASS_URL to your HA address."
    )


_SETPOINT_RE = re.compile(
    r"(?:set|change|make).{0,30}?\b(?:thermostat|temperature|temp|degrees?)\b"
    r".{0,15}?\bto\s+(\d{2})\b"
)
_FILLER_EXTRA = {
    "set", "change", "make", "to", "degrees", "degree",
    "thermostat", "temperature", "temp",
}


def _climate_setpoint(client: HAClient, command: str) -> str | None:
    """Handle 'set the thermostat to 72' — returns spoken text or None."""
    m = _SETPOINT_RE.search(command.lower())
    if not m:
        return None
    temp = int(m.group(1))
    states = client._get("/api/states")
    climates = [s for s in states if s["entity_id"].startswith("climate.")]
    if not climates:
        raise HAError("No thermostat found in Home Assistant.")
    words = re.findall(r"[a-z]+", command.lower())
    tokens = [w for w in words if w not in _FILLER_EXTRA] or ["thermostat"]
    entity = client.resolve(climates, tokens)
    eid = entity["entity_id"]
    friendly = entity["attributes"].get("friendly_name", eid)
    client._post(
        "/api/services/climate/set_temperature",
        {"entity_id": eid, "temperature": temp},
    )
    time.sleep(0.5)
    return f"{friendly} set to {temp} degrees."


@mcp.tool()
def ha(command: str) -> CallToolResult:
    """Control or check Todd's smart home via Home Assistant.

    ALWAYS use this for smart-home requests like "turn on the living room
    lights", "bedroom off", "is the kitchen light on", "set the thermostat
    to 72", "what's the office temperature". command is the spoken request
    in plain text. Handles lights, switches, fans, climate, covers/blinds,
    media players, and sensor/status reads. Returns a short spoken answer.
    Cannot touch locks or alarms from the ring."""
    try:
        c = command.lower()
        if any(k in c for k in _HA_DENY_WORDS):
            return _spoken_response(
                "I can't do locks or alarms from the ring — use the Home Assistant app for that.",
                question=command,
            )
        client = _ha()
        try:
            spoken = _climate_setpoint(client, command)
            if spoken:
                return _spoken_response(spoken, question=command)
        except HAError as e:
            return _spoken_response(str(e), question=command)
        return _spoken_response(client.control(command), question=command)
    except HAError as e:
        return _spoken_response(str(e), question=command)
    except Exception:  # noqa: BLE001
        logger.exception("ha tool failed")
        return _spoken_response("Home Assistant isn't responding right now.",
                                question=command)


# ---------------------------------------------------------------- Wyze

def _wyze_client():
    from wyze_sdk import Client

    missing = [k for k, v in _WYZE.items() if not v]
    if missing:
        raise RuntimeError(f"Wyze credentials missing: {', '.join(missing)}")
    return Client(**_WYZE)


def _wyze_model_map(client) -> dict:
    raw = client._api_client().get_object_list()["data"]["device_list"]
    return {d.get("mac"): d.get("product_model") for d in raw}


@mcp.tool()
def wyze(command: str) -> CallToolResult:
    """Control Todd's two Wyze lights: "Todd's light" (bedroom) and "Lamp"
    (living room) — e.g. "turn off Todd's light", "Lamp on". These are the
    ONLY lights this tool may touch; similarly-named bulbs (Small Lamp,
    Office Lamp, etc.) are never valid targets."""
    try:
        c = command.lower()
        scrubbed = c
        for decoy in _WYZE_DECOYS:
            scrubbed = scrubbed.replace(decoy, " ")
        mac, label = None, None
        if "todd" in scrubbed:
            mac, label = WYZE_TODDS_MAC, "Todd's light"
        elif "lamp" in scrubbed or "living room" in scrubbed:
            mac, label = WYZE_LAMP_MAC, "Lamp"
        if not mac:
            return _spoken_response(
                "I can only control Todd's light and Lamp — which one?",
                question=command,
            )
        has_off = bool(re.search(r"\boff\b|\bout\b", c))
        has_on = bool(re.search(r"\bon\b", c))
        if has_off and not has_on:
            verb, turn = "off", False
        elif has_on and not has_off:
            verb, turn = "on", True
        else:
            return _spoken_response(f"Turn {label} on or off?", question=command)
        client = _wyze_client()
        model = _wyze_model_map(client).get(mac)
        if not model:
            return _spoken_response(f"{label} isn't on the Wyze account right now.",
                                    question=command)
        if turn:
            client.bulbs.turn_on(device_mac=mac, device_model=model)
        else:
            client.bulbs.turn_off(device_mac=mac, device_model=model)
        return _spoken_response(f"{label} is now {verb}.", question=command)
    except RuntimeError as e:
        return _spoken_response(f"Wyze isn't set up yet ({str(e)[:60]}).",
                                question=command)
    except Exception:  # noqa: BLE001
        logger.exception("wyze tool failed")
        return _spoken_response(f"{label or 'The light'} didn't respond.",
                                question=command)


# ---------------------------------------------------------------- ask_muse
# (file-queue bridge to Muse, answered over HTTPS by an off-box agent)


@mcp.tool()
def ask_muse(question: str) -> CallToolResult:
    """Ask Muse anything the ha and wyze tools can't handle — explanations,
    advice, current information, math, opinions, anything needing Todd's
    context or judgment. The question is handed to Muse himself and his
    answer comes back as the phone notification. Keep the question short."""
    _QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    qid = uuid.uuid4().hex
    qfile = _QUEUE_DIR / f"{qid}.json"
    afile = _QUEUE_DIR / f"{qid}.answer.json"
    qfile.write_text(json.dumps({
        "id": qid,
        "question": question,
        "ts": datetime.now(timezone.utc).isoformat(),
    }))
    try:
        deadline = time.time() + _ASK_TIMEOUT_S
        while time.time() < deadline:
            if afile.exists():
                try:
                    answer = (json.loads(afile.read_text() or "{}")
                              .get("answer", "").strip())
                except (json.JSONDecodeError, OSError):
                    answer = ""
                if answer:
                    return _spoken_response(answer, question=question)
            time.sleep(_ASK_POLL_S)
        return _spoken_response(
            "I didn't get an answer in time — ask me in chat instead.",
            question=question,
        )
    finally:
        for f in (qfile, afile):
            try:
                f.unlink(missing_ok=True)
            except OSError:
                pass
        # A stale claim marker is harmless, but keep the dir tidy.
        try:
            (_QUEUE_DIR / f"{qid}.claimed").unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- queue HTTP endpoints
# (same app as /mcp, same bearer auth — lets Muse's cron poll from off-box)


def _queue_question_files():
    """Yield (qid, path) for question files with no answer yet, oldest first."""
    try:
        files = sorted(
            _QUEUE_DIR.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError:
        return
    for p in files:
        name = p.name
        if not name.endswith(".json") or ".answer." in name or ".claimed" in name:
            continue
        qid = name[: -len(".json")]
        if not _QID_RE.match(qid):
            continue
        if (_QUEUE_DIR / f"{qid}.answer.json").exists():
            continue
        if (_QUEUE_DIR / f"{qid}.claimed").exists():
            continue
        yield qid, p


async def queue_pending(request: Request) -> JSONResponse:
    """Return the oldest unanswered, unclaimed question (and claim it)."""
    for qid, p in _queue_question_files():
        try:
            payload = json.loads(p.read_text() or "{}")
        except (json.JSONDecodeError, OSError):
            continue
        try:
            (_QUEUE_DIR / f"{qid}.claimed").write_text(
                datetime.now(timezone.utc).isoformat()
            )
        except OSError:
            continue
        return JSONResponse({"id": qid, **payload})
    return Response(status_code=204)


async def queue_answer(request: Request) -> JSONResponse:
    """Accept {"id": ..., "answer": "..."} and store it for the waiting tool."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    qid = (body.get("id") or "").strip() if isinstance(body, dict) else ""
    answer = (body.get("answer") or "").strip() if isinstance(body, dict) else ""
    if not _QID_RE.match(qid):
        return JSONResponse({"error": "bad id"}, status_code=400)
    if not answer:
        return JSONResponse({"error": "empty answer"}, status_code=400)
    try:
        (_QUEUE_DIR / f"{qid}.answer.json").write_text(
            json.dumps({"answer": answer})
        )
    except OSError:
        return JSONResponse({"error": "write failed"}, status_code=500)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- persona prompt

_MUSE_PERSONA = (
    "You are Muse, Todd's personal AI assistant, answering through his Pebble "
    "Index 01 smart ring. Todd lives in Clairemont, San Diego.\n"
    "Your tools:\n"
    "- `ha`: his smart home — lights, switches, fans, climate, covers/blinds, "
    "media players, sensor reads. ALWAYS use this for home commands, never guess.\n"
    "- `wyze`: ONLY his two Wyze lights — \"Todd's light\" (bedroom) and \"Lamp\" "
    "(living room). Use for on/off of exactly these.\n"
    "- `ask_muse`: anything needing explanations, advice, current information, "
    "or judgment — anything ha/wyze can't do.\n"
    "Rules:\n"
    "- Answer in at most 3 short sentences. No markdown. The answer appears as "
    "a phone notification.\n"
    "- Prefer direct tools over ask for home control.\n"
    "- If you do not know, say so honestly.\n"
    "- Never mention tokens, URLs, file paths, or system internals."
)


def _ring_persona_text() -> str:
    # Operators can override with their own file (name, household layout, etc).
    path = os.environ.get("RING_PERSONA_FILE", "").strip()
    if path:
        try:
            return open(path, encoding="utf-8").read().strip()
        except OSError:
            logger.warning("RING_PERSONA_FILE unreadable (%s); using built-in persona", path)
    return _MUSE_PERSONA


# Registered on the LOW-LEVEL server (not FastMCP's prompt manager) because
# FastMCP serializes `arguments` as an empty list, and the Pebble app's Kotlin
# client drops any prompt whose `arguments` field is a non-null list
# (it checks `arguments == null`). Emitting null keeps ring_persona selectable.
PROMPT_NAME = "ring_persona"
PROMPT_DESCRIPTION = (
    "Persona for the Pebble cloud agent. Select this prompt in the Pebble "
    "app's MCP server settings."
)


async def _handle_list_prompts() -> ListPromptsResult:
    return ListPromptsResult(
        prompts=[
            MCPPrompt(
                name=PROMPT_NAME,
                description=PROMPT_DESCRIPTION,
                arguments=None,
            )
        ]
    )


async def _handle_get_prompt(name: str, arguments: dict | None = None) -> GetPromptResult:
    if name != PROMPT_NAME:
        raise ValueError(f"Unknown prompt: {name}")
    return GetPromptResult(
        messages=[
            PromptMessage(
                role="user",
                content=TextContent(type="text", text=_ring_persona_text()),
            )
        ]
    )


mcp._mcp_server.list_prompts()(_handle_list_prompts)
mcp._mcp_server.get_prompt()(_handle_get_prompt)


def make_app():
    """Return the FastMCP streamable-http app wrapped in bearer-token auth."""
    if not TOKEN:
        logger.warning(
            "MCP_BEARER_TOKEN is empty; every request will be rejected (401). "
            "Set it before deploying."
        )
    inner = mcp.streamable_http_app()
    # Queue bridge endpoints live on the same app (same bearer auth).
    inner.routes.append(Route("/queue/pending", queue_pending, methods=["GET"]))
    inner.routes.append(Route("/queue/answer", queue_answer, methods=["POST"]))
    expected = f"Bearer {TOKEN}"

    async def auth_app(scope, receive, send):
        if scope["type"] != "http":
            return await inner(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        auth = headers.get(b"authorization", b"").decode("latin-1")
        if not TOKEN or not hmac.compare_digest(auth, expected):
            response = JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await response(scope, receive, send)
        return await inner(scope, receive, send)

    auth_app.inner = inner  # exposed for tests (lifespan management)
    return auth_app


app = make_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "pebble_index_mcp.server:app",
        host=os.environ.get("MCP_HOST", "0.0.0.0"),
        port=int(os.environ.get("MCP_PORT", "8765")),
        log_level="info",
    )
