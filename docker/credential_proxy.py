"""Credential Proxy — per-run token → real API key injection.

This service sits between sandbox runner containers and LLM APIs.
The runner only gets a per-run random token (NOT the real API key).
The proxy receives requests from the runner, looks up the real API key
for that token, replaces the Authorization header, and forwards to the
actual LLM API.

Architecture:
  runner ──► credential-proxy:9090 ──► api.deepseek.com / api.anthropic.com
                │
                └── in-memory map: {per_run_token → {api_key, base_url}}

Admin API (for launcher to register keys):
  POST /admin/keys   {run_token, api_key, base_url}
  DELETE /admin/keys/{run_token}
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] credential_proxy: %(message)s",
)
logger = logging.getLogger(__name__)

# ── Key store ──────────────────────────────────────────────
# In-memory mapping: per_run_token → {api_key, base_url}
# Populated by the launcher via the admin API.
KEY_STORE: dict[str, dict[str, str]] = {}

ADMIN_API_TOKEN = os.environ.get("AGENTBOX_API_TOKEN", "dev-token")

# HTTP client for proxying requests
_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=60.0)
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Credential proxy starting (admin token: %s)", "set" if ADMIN_API_TOKEN else "NOT SET")
    yield
    global _client
    if _client:
        await _client.aclose()
        _client = None
    logger.info("Credential proxy stopped")


app = FastAPI(title="credential-proxy", lifespan=lifespan)


# ── Models ─────────────────────────────────────────────────


class RegisterKeyRequest(BaseModel):
    run_token: str = Field(..., description="Per-run token that the sandbox will use")
    api_key: str = Field(..., description="Real LLM API key (never enters the sandbox)")
    base_url: str = Field(..., description="Upstream LLM API base URL (e.g. https://api.deepseek.com)")


class RegisterKeyResponse(BaseModel):
    status: str
    token_prefix: str


# ── Admin API (only accessible from control-plane/launcher) ─


def _verify_admin_token(request: Request) -> None:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = auth[len("Bearer "):]
    if token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


@app.post("/admin/keys", response_model=RegisterKeyResponse)
async def register_key(body: RegisterKeyRequest, request: Request):
    """Register a per-run token → real API key mapping.

    Called by the launcher when a run is claimed.
    The real API key is stored ONLY in this proxy's memory.
    """
    _verify_admin_token(request)

    if body.run_token in KEY_STORE:
        logger.warning("Overwriting existing key for token prefix %s", body.run_token[:8])

    KEY_STORE[body.run_token] = {
        "api_key": body.api_key,
        "base_url": body.base_url.rstrip("/"),
    }
    logger.info(
        "Registered key for token %s... -> %s (%d keys in store)",
        body.run_token[:8],
        body.base_url,
        len(KEY_STORE),
    )
    return RegisterKeyResponse(status="ok", token_prefix=body.run_token[:8])


@app.delete("/admin/keys/{run_token}")
async def delete_key(run_token: str, request: Request):
    """Remove a per-run token mapping (run completed or failed)."""
    _verify_admin_token(request)

    if run_token in KEY_STORE:
        del KEY_STORE[run_token]
        logger.info("Deleted key for token %s... (%d keys remaining)", run_token[:8], len(KEY_STORE))
        return {"status": "ok"}
    logger.warning("Key for token %s... not found, nothing to delete", run_token[:8])
    return {"status": "not_found"}


# ── Proxy handler ──────────────────────────────────────────
# All non-/admin routes are proxied to the LLM API with key injection.


def _extract_bearer_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return None


def _build_upstream_url(path: str, query: str, base_url: str) -> str:
    """Build the full upstream URL from the request path and base URL."""
    # Strip /v1 prefix from path if the base_url already includes it
    if base_url.endswith("/v1") and path.startswith("/v1"):
        path = path[len("/v1"):]
    # Ensure single slash between base_url and path
    base = base_url.rstrip("/")
    path = "/" + path.lstrip("/")
    upstream = f"{base}{path}"
    if query:
        upstream = f"{upstream}?{query}"
    return upstream


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
async def proxy(request: Request, path: str) -> Response:
    """Proxy any request to the LLM API, injecting the real API key."""
    # Extract per-run token from Authorization header
    token = _extract_bearer_token(request)
    if not token:
        logger.warning("Request without Bearer token: %s", request.url.path)
        return Response(
            content='{"error": "Missing Authorization: Bearer <token> header"}',
            status_code=401,
            media_type="application/json",
        )

    # Look up the real API key
    key_entry = KEY_STORE.get(token)
    if key_entry is None:
        logger.warning("Unknown token %s... for path %s", token[:8], request.url.path)
        return Response(
            content='{"error": "Unknown or expired run token"}',
            status_code=403,
            media_type="application/json",
        )

    real_api_key = key_entry["api_key"]
    base_url = key_entry["base_url"]

    # Build upstream URL
    upstream_url = _build_upstream_url(
        path=request.url.path,
        query=request.url.query,
        base_url=base_url,
    )

    # Read request body
    body = await request.body()

    # Build headers: replace Authorization with real key, remove host
    headers = dict(request.headers)
    headers["Authorization"] = f"Bearer {real_api_key}"
    headers.pop("host", None)

    logger.debug("Proxying %s %s", request.method, upstream_url)

    try:
        client = get_client()
        upstream_resp = await client.request(
            method=request.method,
            url=upstream_url,
            headers=headers,
            content=body,
        )

        # Build response
        response_headers = dict(upstream_resp.headers)
        # Remove hop-by-hop headers
        for hop_header in ["transfer-encoding", "connection", "keep-alive", "proxy-authenticate",
                           "proxy-authorization", "te", "trailer", "upgrade"]:
            response_headers.pop(hop_header, None)

        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=response_headers,
        )

    except httpx.TimeoutException:
        logger.error("Timeout proxying to %s", upstream_url)
        return Response(
            content='{"error": "Upstream LLM API timeout"}',
            status_code=504,
            media_type="application/json",
        )
    except httpx.RequestError as exc:
        logger.error("Error proxying to %s: %s", upstream_url, exc)
        return Response(
            content=f'{{"error": "Upstream request failed: {exc}"}}',
            status_code=502,
            media_type="application/json",
        )


def main() -> None:
    import uvicorn

    port = int(os.environ.get("CREDENTIAL_PROXY_PORT", "9090"))
    logger.info("Starting credential proxy on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
