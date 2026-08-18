#!/usr/bin/env python
"""Register the trading-desk MCP server with a running Paperclip instance.

Paperclip has no config file for MCP servers: applications, connections, access
profiles and bindings are all created through its REST API. This script drives
the same "connect wizard" flow the Paperclip UI uses.

Every endpoint and payload below was verified against Paperclip 2026.722.0
running locally. Notes on what differs from the prose docs:

  * transport enum is `mcp_remote` (not `remote_http`); the full set is
    `mcp_remote | rest_api | local_stdio`.
  * profile entries use a typed field per selector — `toolName` for
    `selectorType: "tool_name"`, `riskLevel` for `selectorType: "risk_level"` —
    not a generic `selectorValue`.
  * runtime operations (health check, catalog refresh, enable) live on
    `/api/tool-connections/{id}`, NOT nested under the company.

**Your MCP server must run in stateless HTTP mode.** Paperclip's catalog refresh
issues a bare `tools/list` with no `initialize` handshake; a stateful FastMCP
server rejects that with "Missing session ID". `tradingdesk serve` is stateless
by default.

    python scripts/register_paperclip.py                 # show the plan
    python scripts/register_paperclip.py --apply         # create everything

Environment:
    PAPERCLIP_URL         default http://127.0.0.1:3100
    PAPERCLIP_COMPANY_ID  required for --apply
    PAPERCLIP_API_TOKEN   optional; loopback local_trusted needs no auth
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

# Tool bands mirroring the prefixes in tradingdesk/mcp_server.py. The Trader is
# the only role granted the trading tools; the Risk Manager gets reads only.
PROFILES: list[dict[str, Any]] = [
    {
        "profileKey": "desk.analyst",
        "name": "Trading desk — analyst",
        "defaultAction": "deny",
        "entries": [
            {"selectorType": "tool_name", "toolName": "research_watchlist", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "research_snapshot", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "research_screen", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "research_price_history", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "research_add_to_watchlist", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "predict_record", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "predict_list", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "predict_scorecard", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "desk_publish_report", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "desk_status", "effect": "include"},
        ],
    },
    {
        "profileKey": "desk.pm",
        "name": "Trading desk — head of research",
        "defaultAction": "deny",
        "entries": [
            {"selectorType": "tool_name", "toolName": "research_*", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "predict_*", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "desk_*", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "book_portfolio", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "book_performance", "effect": "include"},
            # The PM reviews and instructs; it never moves the book itself.
            {"selectorType": "tool_name", "toolName": "book_submit_order", "effect": "exclude"},
            {"selectorType": "tool_name", "toolName": "book_close_position", "effect": "exclude"},
        ],
    },
    {
        "profileKey": "desk.trader",
        "name": "Trading desk — trader",
        "defaultAction": "deny",
        "entries": [
            {"selectorType": "tool_name", "toolName": "book_*", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "predict_list", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "research_snapshot", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "desk_status", "effect": "include"},
        ],
    },
    {
        "profileKey": "desk.risk",
        "name": "Trading desk — risk manager (read only)",
        "defaultAction": "deny",
        "entries": [
            {"selectorType": "risk_level", "riskLevel": "read", "effect": "include"},
            {"selectorType": "tool_name", "toolName": "desk_publish_report", "effect": "include"},
        ],
    },
]


class PaperclipError(RuntimeError):
    pass


class Paperclip:
    def __init__(self, base: str, token: str = "") -> None:
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, payload: Optional[dict] = None) -> tuple[int, Any]:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, raw
        except urllib.error.URLError as exc:
            raise PaperclipError(f"cannot reach {self.base}: {exc.reason}") from exc

    def expect(self, method: str, path: str, payload: Optional[dict] = None) -> Any:
        status, data = self.call(method, path, payload)
        if status >= 300:
            raise PaperclipError(f"{method} {path} -> {status}\n{json.dumps(data, indent=2)[:800]}")
        return data


def register(client: Paperclip, company: str, desk_url: str, app_name: str) -> None:
    base = f"/api/companies/{company}/tools"

    # 1. Connect. This pings the server, creates the application + connection,
    #    and imports the tool catalog in one call.
    result = client.expect("POST", f"{base}/apps/connect", {"link": desk_url, "name": app_name})
    connection_id = result["connectionId"]
    read_only = result.get("actions", {}).get("readOnly", [])
    mutating = result.get("actions", {}).get("canMakeChanges", [])

    print(f"connected: {connection_id}")
    print(f"  {len(read_only)} read-only, {len(mutating)} mutating tools discovered")
    for entry in mutating:
        print(f"    [{entry.get('riskLevel')}] {entry.get('toolName')}")

    # 2. Finish: enable the catalog, and put every write/destructive tool behind
    #    an approval gate. Trading tools are annotated destructive by the desk,
    #    so orders land in ask-first rather than running unattended on day one.
    enabled = [e["catalogEntryId"] for e in read_only + mutating]
    ask_first = [
        e["catalogEntryId"] for e in mutating if e.get("riskLevel") in ("write", "destructive")
    ]
    client.expect(
        "POST",
        f"{base}/apps/{connection_id}/finish",
        {
            "enabledCatalogEntryIds": enabled,
            "askFirstCatalogEntryIds": ask_first,
            "access": "all_agents",
        },
    )
    print(f"  enabled {len(enabled)} tools, {len(ask_first)} behind approval")

    # 3. Access profiles, one per desk role.
    for profile in PROFILES:
        status, data = client.call("POST", f"{base}/profiles", profile)
        if status == 409:
            print(f"  profile {profile['profileKey']}: already exists, skipped")
            continue
        if status >= 300:
            raise PaperclipError(
                f"profile {profile['profileKey']} -> {status}\n{json.dumps(data, indent=2)[:500]}"
            )
        print(f"  profile {profile['profileKey']}: {data['id']}")

    print(
        "\nNext: bind each profile to its agent — Tools -> Profiles -> Bind in the UI, or\n"
        f"  POST {base}/profiles/{{profileId}}/bind "
        '{"targetType":"agent","targetId":"<AGENT_ID>","priority":10}'
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--apply", action="store_true", help="actually create (default is a dry run)")
    parser.add_argument(
        "--desk-url",
        default=os.environ.get("TRADINGDESK_PUBLIC_URL", "http://127.0.0.1:8010/mcp"),
        help="URL Paperclip should reach the desk MCP server on",
    )
    parser.add_argument("--name", default="Trading Desk (OpenBB)", help="application name in Paperclip")
    parser.add_argument(
        "--paperclip-url", default=os.environ.get("PAPERCLIP_URL", "http://127.0.0.1:3100")
    )
    parser.add_argument("--company-id", default=os.environ.get("PAPERCLIP_COMPANY_ID", ""))
    args = parser.parse_args(argv)

    if not args.apply:
        print("DRY RUN — nothing will be sent.\n")
        print(f"Would connect {args.desk_url} to {args.paperclip_url} as {args.name!r}:\n")
        print(f"  POST /api/companies/{{company}}/tools/apps/connect")
        print(f"       {json.dumps({'link': args.desk_url, 'name': args.name})}")
        print("  POST /api/companies/{company}/tools/apps/{connectionId}/finish")
        print("       enable the catalog; write+destructive tools go behind approval")
        for profile in PROFILES:
            print(f"  POST /api/companies/{{company}}/tools/profiles   ({profile['profileKey']})")
        print(
            "\nPrerequisites:"
            "\n  * the desk MCP server is running and reachable  (tradingdesk serve)"
            "\n  * it is in STATELESS http mode — the default; Paperclip's catalog"
            "\n    refresh sends a bare tools/list with no initialize handshake"
            "\n\nRe-run with --apply and PAPERCLIP_COMPANY_ID set."
        )
        return 0

    if not args.company_id:
        print("error: set PAPERCLIP_COMPANY_ID (or pass --company-id) before --apply", file=sys.stderr)
        return 2

    client = Paperclip(args.paperclip_url, os.environ.get("PAPERCLIP_API_TOKEN", ""))
    try:
        register(client, args.company_id, args.desk_url, args.name)
    except PaperclipError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
