#!/usr/bin/env python3
import os
import logging
from typing import Optional, Dict, Any
from urllib.parse import parse_qs

import redis
import base64
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("ts43-issue-auth-code")

# For self-signed 
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")

# HTTP timeouts
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

# in-cluster Kong proxy
KONG_INTERNAL_BASE = os.getenv(
    "OAUTH_BASE_URL",
    "https://kong-kong-proxy.kong.svc.cluster.local:443"
).strip()

# --- NEW: Redis Configuration ---
REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD") 
# Use this if your Redis requires SSL, common in managed services
REDIS_SSL = os.getenv("REDIS_SSL", "false").lower() in ("1", "true", "yes")

app = FastAPI(title="ts43-issue-auth-code", version="1.0.0")


class HealthzFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return 'GET /healthz ' not in msg and 'GET /healthz HTTP' not in msg

uvicorn_access = logging.getLogger("uvicorn.access")
uvicorn_access.addFilter(HealthzFilter())


def get_client_credentials_token(
    kong_base_url: str,
    client_id: str,
    client_secret: str,
    scope: Optional[str] = None,
    verify_tls: bool = True,
    timeout_sec: float = 10.0,
) -> Dict[str, Any]:
    """
    (This function remains the same)
    Call Kong OAuth2 plugin token endpoint to VALIDATE credentials.
    """
    if not kong_base_url:
        raise ValueError("kong_base_url is required")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")

    url = kong_base_url.rstrip("/") + "/oauth2/token"  # Corrected path
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope

    log.info(f"Validating credentials against: {url}")
    try:
        resp = requests.post(url, data=data, headers=headers, timeout=timeout_sec, verify=verify_tls)
        if resp.status_code >= 400:
            log.error("Credential validation failed: %s %s", resp.status_code, resp.text)
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        payload = resp.json()
        if "access_token" not in payload:
            log.error("Token response missing 'access_token': %s", payload)
            raise HTTPException(status_code=502, detail="Invalid token response from Kong")
        return payload
    except requests.RequestException as e:
        log.exception("Token request error")
        raise HTTPException(status_code=502, detail=f"Token request error: {e}")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


def _flatten_form(form) -> Dict[str, str]:
    """(This function remains the same)"""
    out: Dict[str, str] = {}
    for key in form.keys():
        vals = form.getlist(key)
        out[key] = (vals[0] if vals else "").strip()
    return out


@app.post("/v2/issue_auth_code")
async def issue_auth_code(req: Request):
    """
    (Parsing logic remains the same)
    """
    content_type = (req.headers.get("content-type") or "").lower()
    parsed: Dict[str, Any] = {}
    try:
        if "application/json" in content_type: parsed = await req.json()
        elif "application/x-www-form-urlencoded" in content_type: parsed = _flatten_form(await req.form())
    except Exception:
        pass # Fallback if parsing fails

    grant_type = (req.headers.get("grant_type") or parsed.get("grant_type") or "").strip()
    client_id = (req.headers.get("client_id") or parsed.get("client_id") or "").strip()
    client_secret = (req.headers.get("client_secret") or parsed.get("client_secret") or "").strip()
    scope = (req.headers.get("scope") or parsed.get("scope") or "").strip() or None
    
    log.info(f"client_id: {client_id}")
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id / client_secret missing")
    if grant_type and grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    # --- MODIFIED LOGIC ---

    # Step 1: Validate credentials by requesting a token from Kong.
    # We will discard the token, as its successful creation is our validation.
    get_client_credentials_token(
        kong_base_url=KONG_INTERNAL_BASE,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        verify_tls=VERIFY_TLS,
        timeout_sec=REQ_TIMEOUT,
    )
    log.info("Client credentials successfully validated against Kong.")

    # Step 2: Prepare credentials for storage in Redis.
    combined_creds = f"{client_id}:{client_secret}"
    encoded_creds = base64.b64encode(combined_creds.encode('utf-8')).decode('utf-8')

    # Step 3: Generate a temporary auth code and store the credentials in Redis.
    auth_code = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip("=")
    redis_key = f"auth_code:{auth_code}"
    
    if not REDIS_HOST:
        log.error("REDIS_HOST environment variable is not set. Cannot store auth code.")
        raise HTTPException(status_code=500, detail="Service is misconfigured (Redis host missing)")

    try:
        log.info(f"Connecting to Redis at {REDIS_HOST}:{REDIS_PORT}")
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=int(REDIS_PORT),
            password=REDIS_PASSWORD,
            ssl=REDIS_SSL,
            decode_responses=True,
            socket_connect_timeout=5
        )
        # Store the encoded credentials with a 120-second expiry
        redis_client.setex(redis_key, 120, encoded_creds)
        log.info(f"Successfully stored auth code in Redis with key: {redis_key}")
    except Exception as e:
        log.exception("Failed to store auth code in Redis.")
        raise HTTPException(status_code=503, detail=f"Service unavailable: could not connect to Redis: {e}")

    # Step 4: Return the temporary auth code to the client.
    return JSONResponse({
        "auth_code": auth_code,
        "expires_in": 120
    })