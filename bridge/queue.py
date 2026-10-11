#!/usr/bin/env python3
"""Poll or answer Todd's Pi ring queue. Reads QUEUE_BASE_URL + MCP_BEARER_TOKEN
from ~/.config/pebble-index-mcp/pi.env (mode 600). Never prints the token."""
import json
import os
import sys
import urllib.request


def _cfg():
    cfg = {}
    with open(os.path.expanduser("~/.config/pebble-index-mcp/pi.env")) as f:
        for line in f:
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    if not cfg.get("QUEUE_BASE_URL") or not cfg.get("MCP_BEARER_TOKEN"):
        print("NOT_CONFIGURED")
        sys.exit(2)
    return cfg


def _req(cfg, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    url = cfg["QUEUE_BASE_URL"].rstrip("/") + path
    # Tailscale IPs need the tailnet proxy (port 3130)
    handlers = []
    if "/100." in url or "//100." in url:
        proxy_base = os.environ.get("HTTPS_PROXY", os.environ.get("https_proxy", ""))
        if proxy_base:
            # Swap egress proxy port for the tailnet proxy port
            tailnet_proxy = proxy_base.rsplit(":", 1)[0] + ":3130"
            handlers.append(urllib.request.ProxyHandler({"http": tailnet_proxy, "https": tailnet_proxy}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()
    r = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + cfg["MCP_BEARER_TOKEN"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with opener.open(r, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:200]


def main():
    cfg = _cfg()
    if sys.argv[1] == "poll":
        status, body = _req(cfg, "GET", "/queue/pending")
        if status == 204:
            print("EMPTY")
        elif status == 200:
            print(body)
        else:
            print(f"ERROR {status}")
            sys.exit(1)
    elif sys.argv[1] == "answer":
        qid, answer = sys.argv[2], sys.argv[3]
        status, _ = _req(cfg, "POST", "/queue/answer", {"id": qid, "answer": answer})
        print("OK" if status == 200 else f"ERROR {status}")
        sys.exit(0 if status == 200 else 1)


main()
