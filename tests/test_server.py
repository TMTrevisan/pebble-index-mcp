import importlib
import json
import types
from contextlib import asynccontextmanager

import httpx
import pytest


@pytest.fixture()
def app_with_token(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


@asynccontextmanager
async def app_lifespan(app):
    """Enter the inner Starlette app's lifespan so FastMCP's task group initializes."""
    inner = app.inner
    ctx = inner.router.lifespan_context(inner)
    await ctx.__aenter__()
    try:
        yield
    finally:
        await ctx.__aexit__(None, None, None)


INIT_PAYLOAD = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0.1"},
    },
}


def _client(app):
    # Host must include a port: FastMCP's DNS-rebinding defaults allow
    # "127.0.0.1:*" patterns, and a portless Host fails the check (421).
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    )


async def test_unauthorized_no_header(app_with_token):
    async with _client(app_with_token) as client:
        r = await client.post("/mcp", json=INIT_PAYLOAD)
    assert r.status_code == 401


async def test_unauthorized_bad_token(app_with_token):
    async with _client(app_with_token) as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={"Authorization": "Bearer wrong"},
        )
    assert r.status_code == 401


@pytest.fixture()
def app_empty_token(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


@pytest.fixture()
def app_with_host(monkeypatch):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    monkeypatch.setenv("MCP_ALLOWED_HOSTS", "ring.example.com")
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv.make_app()


async def test_initialize_with_token(app_with_token):
    async with app_lifespan(app_with_token):
        async with _client(app_with_token) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert r.status_code == 200


async def test_empty_token_denies_all(app_empty_token):
    async with _client(app_empty_token) as client:
        r = await client.post(
            "/mcp",
            json=INIT_PAYLOAD,
            headers={"Authorization": "Bearer anything"},
        )
    assert r.status_code == 401


async def test_unallowed_host_rejected(app_with_token):
    async with app_lifespan(app_with_token):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_with_token),
            base_url="http://evil.example.com:8765",
        ) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={"Authorization": "Bearer sekret"},
            )
    assert r.status_code == 421


async def test_configured_host_allowed(app_with_host):
    async with app_lifespan(app_with_host):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_with_host),
            base_url="http://ring.example.com",
        ) as client:
            r = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
    assert r.status_code == 200


# ---------------------------------------------------------------- unit tests

def _unit_srv(monkeypatch, **env):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "sekret")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import pebble_index_mcp.server as srv

    importlib.reload(srv)
    return srv


def _check_contract(r):
    """Every user-facing answer must carry the Pebble coreSchema contract."""
    assert r.meta == {"coreSchema": 1}
    sc = r.structuredContent
    assert sc["semanticResult"]["type"] == "Response"
    assert sc["output"] == r.content[0].text


def test_ha_denies_lock_and_alarm_commands(monkeypatch):
    srv = _unit_srv(monkeypatch)

    def boom():
        raise AssertionError("HA must not be reached for locks/alarms")

    monkeypatch.setattr(srv, "_ha", boom)
    r = srv.ha("unlock the front door")
    _check_contract(r)
    assert "can't do locks or alarms" in r.content[0].text

    r = srv.ha("disarm the alarm")
    assert "can't do locks or alarms" in r.content[0].text


def test_ha_control_success(monkeypatch):
    srv = _unit_srv(monkeypatch, HASS_TOKEN="x")

    class FakeHA:
        def control(self, command):
            assert command == "turn on the living room"
            return "Living Room is now on."

    monkeypatch.setattr(srv, "_ha", lambda: FakeHA())
    r = srv.ha("turn on the living room")
    _check_contract(r)
    assert r.content[0].text == "Living Room is now on."
    assert r.structuredContent["semanticResult"] == {
        "type": "Response",
        "text": "Living Room is now on.",
        "question": "turn on the living room",
    }


def test_ha_climate_setpoint(monkeypatch):
    srv = _unit_srv(monkeypatch, HASS_TOKEN="x")
    calls = {}

    class FakeHA:
        def _get(self, path):
            return [{
                "entity_id": "climate.thermostat",
                "state": "heat",
                "attributes": {"friendly_name": "Thermostat"},
            }]

        def resolve(self, states, tokens):
            return states[0]

        def _post(self, path, payload):
            calls["path"] = path
            calls["payload"] = payload
            return {}

    monkeypatch.setattr(srv, "_ha", lambda: FakeHA())
    r = srv.ha("set the thermostat to 72")
    _check_contract(r)
    assert calls["path"] == "/api/services/climate/set_temperature"
    assert calls["payload"] == {"entity_id": "climate.thermostat", "temperature": 72}
    assert "72 degrees" in r.content[0].text


def test_ha_missing_token_is_graceful(monkeypatch):
    srv = _unit_srv(monkeypatch)  # no HASS_TOKEN
    r = srv.ha("turn on the kitchen")
    _check_contract(r)
    assert "HASS_TOKEN" in r.content[0].text


def _fake_wyze(monkeypatch, srv):
    calls = []

    class FakeBulbs:
        def turn_on(self, device_mac, device_model):
            calls.append(("on", device_mac, device_model))

        def turn_off(self, device_mac, device_model):
            calls.append(("off", device_mac, device_model))

    class FakeClient:
        bulbs = FakeBulbs()

    monkeypatch.setattr(srv, "_wyze_client", lambda: FakeClient())
    monkeypatch.setattr(
        srv, "_wyze_model_map",
        lambda client: {"D03F27102015": "WLPA19", "2CAA8E560872": "WLPA19"},
    )
    return calls


def test_wyze_todds_light_off(monkeypatch):
    srv = _unit_srv(monkeypatch)
    calls = _fake_wyze(monkeypatch, srv)
    r = srv.wyze("turn off Todd's light")
    _check_contract(r)
    assert calls == [("off", "D03F27102015", "WLPA19")]
    assert r.content[0].text == "Todd's light is now off."


def test_wyze_lamp_on(monkeypatch):
    srv = _unit_srv(monkeypatch)
    calls = _fake_wyze(monkeypatch, srv)
    r = srv.wyze("turn on the lamp")
    assert calls == [("on", "2CAA8E560872", "WLPA19")]
    assert r.content[0].text == "Lamp is now on."


def test_wyze_decoy_lamp_does_not_match(monkeypatch):
    """The similarly-named decoy bulbs must never be treated as Todd's Lamp."""
    srv = _unit_srv(monkeypatch)
    calls = _fake_wyze(monkeypatch, srv)
    r = srv.wyze("turn on the small lamp")
    assert calls == []  # decoy scrubbed, no valid target
    assert r.content[0].text == "I can only control Todd's light and Lamp — which one?"


def test_wyze_ambiguous_toggle_asks_on_or_off(monkeypatch):
    srv = _unit_srv(monkeypatch)
    calls = _fake_wyze(monkeypatch, srv)
    r = srv.wyze("toggle the lamp")
    assert calls == []
    assert r.content[0].text == "Turn Lamp on or off?"


def test_wyze_missing_creds_is_graceful(monkeypatch):
    srv = _unit_srv(monkeypatch)  # no WYZE_* env
    r = srv.wyze("turn on the lamp")
    _check_contract(r)
    assert "isn't set up yet" in r.content[0].text


def test_ask_muse_returns_answer(monkeypatch, tmp_path):
    """ask_muse writes a question file; when the answer file appears the
    tool returns it through the contract."""
    import threading

    srv = _unit_srv(monkeypatch)
    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    monkeypatch.setattr(srv, "_ASK_POLL_S", 0.05)

    def answerer():
        for _ in range(200):
            qf = [f for f in tmp_path.glob("*.json")
                  if not f.name.endswith(".answer.json")]
            if qf:
                qid = qf[0].stem
                (tmp_path / f"{qid}.answer.json").write_text(
                    json.dumps({"answer": "Paris is the capital of France."})
                )
                return
            threading.Event().wait(0.05)
        raise AssertionError("question file never appeared")

    t = threading.Thread(target=answerer, daemon=True)
    t.start()
    r = srv.ask_muse("what is the capital of france")
    t.join(timeout=10)
    _check_contract(r)
    assert r.content[0].text == "Paris is the capital of France."
    assert (
        r.structuredContent["semanticResult"]["question"]
        == "what is the capital of france"
    )
    # queue cleaned up
    assert list(tmp_path.glob("*.json")) == []


def test_ask_muse_timeout_is_graceful(monkeypatch, tmp_path):
    srv = _unit_srv(monkeypatch)
    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    monkeypatch.setattr(srv, "_ASK_TIMEOUT_S", 0.1)
    monkeypatch.setattr(srv, "_ASK_POLL_S", 0.05)
    r = srv.ask_muse("will anyone answer this")
    _check_contract(r)
    assert "didn't get an answer in time" in r.content[0].text
    assert list(tmp_path.glob("*.json")) == []


# ---------------------------------------------------------------- queue HTTP endpoints


def _queue_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
    )


def _queue_headers(token="sekret"):
    return {"Authorization": f"Bearer {token}", "Host": "127.0.0.1:8765"}


async def test_queue_pending_empty_returns_204(app_with_token, monkeypatch, tmp_path):
    import pebble_index_mcp.server as srv

    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    async with _queue_client(app_with_token) as client:
        r = await client.get("/queue/pending", headers=_queue_headers())
    assert r.status_code == 204


async def test_queue_pending_requires_auth(app_with_token, monkeypatch, tmp_path):
    import pebble_index_mcp.server as srv

    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    async with _queue_client(app_with_token) as client:
        r = await client.get("/queue/pending")
        assert r.status_code == 401
        r = await client.get("/queue/pending", headers=_queue_headers("wrong"))
        assert r.status_code == 401
        r = await client.post("/queue/answer", json={"id": "x", "answer": "y"})
        assert r.status_code == 401


async def test_queue_claim_answer_flow(app_with_token, monkeypatch, tmp_path):
    """pending returns oldest question and claims it; answer stores the file."""
    import pebble_index_mcp.server as srv

    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    qid = "a" * 32
    (tmp_path / f"{qid}.json").write_text(
        json.dumps({"id": qid, "question": "q?", "ts": "t"})
    )
    async with _queue_client(app_with_token) as client:
        r = await client.get("/queue/pending", headers=_queue_headers())
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == qid
        assert body["question"] == "q?"
        # claimed: second poll finds nothing
        assert (tmp_path / f"{qid}.claimed").exists()
        r = await client.get("/queue/pending", headers=_queue_headers())
        assert r.status_code == 204
        # post the answer
        r = await client.post(
            "/queue/answer",
            headers=_queue_headers(),
            json={"id": qid, "answer": "the answer"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
    stored = json.loads((tmp_path / f"{qid}.answer.json").read_text())
    assert stored["answer"] == "the answer"


async def test_queue_answer_rejects_bad_input(app_with_token, monkeypatch, tmp_path):
    import pebble_index_mcp.server as srv

    monkeypatch.setattr(srv, "_QUEUE_DIR", tmp_path)
    async with _queue_client(app_with_token) as client:
        # path traversal attempt
        r = await client.post(
            "/queue/answer",
            headers=_queue_headers(),
            json={"id": "../../evil", "answer": "x"},
        )
        assert r.status_code == 400
        # empty answer
        r = await client.post(
            "/queue/answer",
            headers=_queue_headers(),
            json={"id": "b" * 32, "answer": "  "},
        )
        assert r.status_code == 400


async def test_prompts_list_arguments_null(app_with_token):
    """The Pebble app hides prompts whose `arguments` field is a non-null
    list, so the wire format must carry arguments: null."""
    import json

    async with app_lifespan(app_with_token):
        async with _client(app_with_token) as client:
            init = await client.post(
                "/mcp",
                json=INIT_PAYLOAD,
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                },
            )
            sid = init.headers.get("mcp-session-id")
            assert sid, "initialize did not return an mcp-session-id"
            r = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
                headers={
                    "Authorization": "Bearer sekret",
                    "Accept": "application/json, text/event-stream",
                    "mcp-session-id": sid,
                },
            )
    assert r.status_code == 200
    data_line = [l for l in r.text.splitlines() if l.startswith("data:")][0][5:].strip()
    payload = json.loads(data_line)
    prompts = payload["result"]["prompts"]
    assert [p["name"] for p in prompts] == ["ring_persona"]
    # The Pebble client treats a missing or null `arguments` field the same
    # way (null); an empty list hides the prompt. Accept both wire forms.
    assert prompts[0].get("arguments") is None
