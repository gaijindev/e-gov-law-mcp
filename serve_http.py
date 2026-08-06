"""HTTP entrypoint for deploying this server (e.g. behind Dokploy/Traefik).

Additive only — server.py's stdio entrypoint (for Claude Desktop/Code local
config) is untouched. This just exposes the same `mcp` FastMCP instance over
Streamable HTTP.

By default this refuses to start without a bearer token, since an unmarked
open port looks like a mistake. But the data behind this server (Japan's
e-Gov 法令 statute text) is public and free with no API key of its own, so an
authless deployment is a legitimate choice — e.g. to serve ChatGPT/ Claude
connectors or the MCP directory, which expect public-data servers to be
reachable without auth. Opt into that explicitly with `LAW_MCP_PUBLIC=1`.

Env vars:
  LAW_MCP_TOKEN   bearer token; requests without a matching
                  `Authorization: Bearer <token>` header get 401.
  LAW_MCP_PUBLIC  set to "1" to run without any auth middleware (authless,
                  public statute data). Mutually exclusive with
                  LAW_MCP_TOKEN in the sense that if both are set, the token
                  still wins and auth stays on.
  PORT            defaults to 8000 (Dokploy/Traefik convention).
"""
import os

import uvicorn
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from server import mcp

TOKEN = os.environ.get("LAW_MCP_TOKEN")
PUBLIC = os.environ.get("LAW_MCP_PUBLIC") == "1"


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if TOKEN:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    if not TOKEN and not PUBLIC:
        raise SystemExit(
            "Neither LAW_MCP_TOKEN nor LAW_MCP_PUBLIC=1 is set. Refusing to "
            "start unauthenticated on a public port. Either set LAW_MCP_TOKEN "
            "(a long random string), or set LAW_MCP_PUBLIC=1 to explicitly run "
            "authless (this server only serves public Japanese statute text, "
            "so that's a legitimate choice for e.g. ChatGPT/Claude connectors)."
        )
    if TOKEN:
        app = mcp.http_app(middleware=[Middleware(BearerAuthMiddleware)])
    else:
        print("e-gov-law-mcp: running authless — public statute data, no auth "
              "required (LAW_MCP_PUBLIC=1).")
        app = mcp.http_app()
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
