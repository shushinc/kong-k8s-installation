#!/usr/bin/env python3
import os
import logging
from typing import Optional, Dict, Any
from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import uvicorn

# -----------------------
# Logging
# -----------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("ts43-issue-auth-code")

# For self-signed / NodePort TLS you may want to disable verification (NOT for prod)
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")

# HTTP timeouts
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))

# in-cluster Kong proxy by default (can override with OAUTH_BASE_URL if needed)
# KONG_INTERNAL_BASE = os.getenv(
#     "OAUTH_BASE_URL",
#     "http://kong-kong-proxy.kong.svc.cluster.local:80"
# ).strip() 
# change to HTTPS for security error comes from kong plugin
KONG_INTERNAL_BASE = os.getenv(
    "OAUTH_BASE_URL",
    "https://kong-kong-proxy.kong.svc.cluster.local:443"
).strip()

app = FastAPI(title="ts43-issue-auth-code", version="1.0.0")


class HealthzFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return 'GET /healthz ' not in msg and 'GET /healthz HTTP' not in msg


# Attach to uvicorn access logger (hide /healthz noise)
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
    Call Kong OAuth2 plugin token endpoint:
      POST {kong_base_url}/oauth2/token/oauth2/token
    Returns dict: { access_token, token_type, expires_in, ... }
    """
    if not kong_base_url:
        raise ValueError("kong_base_url is required")
    if not client_id or not client_secret:
        raise ValueError("client_id and client_secret are required")

    # Correct path and preserve scheme from kong_base_url
    url = kong_base_url.rstrip("/") + "/oauth2/token/oauth2/token"  
    headers = {"X-Forwarded-Proto": "https"}
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    if scope:
        data["scope"] = scope

    log.info(f"Requesting token from: {url}")
    log.info(f"headers: {headers}")
    try:
        
        resp = requests.post(url, data=data, headers=headers,timeout=timeout_sec, verify=verify_tls)
        if resp.status_code >= 400:
            log.error("Token request failed: %s %s", resp.status_code, resp.text)
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
    """Convert Starlette FormData to dict (take first value per key)."""
    out: Dict[str, str] = {}
    for key in form.keys():
        vals = form.getlist(key)
        out[key] = (vals[0] if vals else "").strip()
    return out


@app.post("/v2/issue_auth_code")
async def issue_auth_code(req: Request):
    """
    Accepts JSON or application/x-www-form-urlencoded.
    Values are taken from headers first (set by Kong), then fall back to body.
    Expected:
      - client_id      (required)
      - client_secret  (required)
      - scope          (optional)
      - grant_type     (optional; validated if present)
    """
    # --- Debug logging (careful in prod; secrets!) ---
    print("=== HEADERS ===")
    for k, v in req.headers.items():
        print(f"{k}: {v}")

    body_bytes = await req.body()
    raw_text = body_bytes.decode("utf-8", errors="replace")
    print("=== BODY (raw) ===")
    print(raw_text if raw_text else "<empty>")

    # --- Parse body (best-effort) ---
    content_type = (req.headers.get("content-type") or "").lower()
    parsed: Dict[str, Any] = {}
    try:
        if "application/json" in content_type:
            parsed = await req.json()
        elif "application/x-www-form-urlencoded" in content_type:
            # Requires python-multipart; otherwise falls into except below.
            form = await req.form()
            parsed = _flatten_form(form)
        else:
            try:
                parsed = await req.json()
            except Exception:
                qs = parse_qs(raw_text, keep_blank_values=True)
                parsed = {k: (v[0] if isinstance(v, list) and v else "") for k, v in qs.items()}
    except Exception as e:
        log.warning(f"Body parse failed ({e}); proceeding with empty body")
        parsed = {}

    # --- Prefer headers, then fallback to parsed body ---
    grant_type   = (req.headers.get("grant_type") or parsed.get("grant_type") or "").strip()
    client_id    = (req.headers.get("client_id") or parsed.get("client_id") or "").strip()
    client_secret= (req.headers.get("client_secret") or parsed.get("client_secret") or "").strip()
    scope        = (req.headers.get("scope") or parsed.get("scope") or "").strip() or None

    # in-cluster Kong base (ignore incoming host)
    kong_base = KONG_INTERNAL_BASE 

    # --- Logging (mask secret in logs) ---
    log.info(f"client_id: {client_id}")
    log.info(f"client_secret: {client_secret[:2] + '***' if client_secret else ''}")
    log.info(f"scope: {scope}")
    log.info(f"kong_base: {kong_base}")

    # --- Validate ---
    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="client_id / client_secret missing")
    if grant_type and grant_type != "client_credentials":
        raise HTTPException(status_code=400, detail="unsupported_grant_type")

    # --- Call Kong /oauth2/token ---
    token = get_client_credentials_token(
        kong_base_url=kong_base,
        client_id=client_id,
        client_secret=client_secret,
        scope=scope,
        verify_tls=VERIFY_TLS,
        timeout_sec=REQ_TIMEOUT,
    )
    return JSONResponse(token)

# (Note: there is duplicate unreachable code after this return in your original file;
# leaving it untouched since you asked for only the in-cluster base changes.)
