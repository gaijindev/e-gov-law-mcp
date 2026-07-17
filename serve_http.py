"""HTTP entrypoint for deploying this server (e.g. behind Dokploy/Traefik).

Additive only — server.py's stdio entrypoint (for Claude Desktop/Code local
config) is untouched. This just exposes the same `mcp` FastMCP instance over
Streamable HTTP, gated by a shared-secret bearer token, so a hosted instance
isn't open to the public internet.

Env vars:
  LAW_MCP_TOKEN   required bearer token; requests without a matching
                  `Authorization: Bearer <token>` header get 401.
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


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if TOKEN:
            header = request.headers.get("authorization", "")
            if header != f"Bearer {TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "LAW_MCP_TOKEN is not set. Refusing to start unauthenticated on a "
            "public port. Set it (a long random string) before deploying."
        )
    app = mcp.http_app(middleware=[Middleware(BearerAuthMiddleware)])
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
