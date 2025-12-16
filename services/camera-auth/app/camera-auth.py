#!/usr/bin/env python3
import os
import logging
import time
import base64
import json
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Tuple, Optional

import requests
import redis
from fastapi import FastAPI, Response, Form, HTTPException, Header, Request
from fastapi.responses import JSONResponse

# =========================
# Logging configuration
# =========================
def _configure_logging() -> logging.Logger:
    # Default level INFO; respect LOG_LEVEL if provided
    log_level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level = getattr(logging, log_level_name, logging.INFO)

    # Root logger (so uvicorn/fastapi libs can also inherit format)
    logger = logging.getLogger()
    logger.setLevel(log_level)

    # Clear existing handlers to avoid duplicates on reload
    for h in list(logger.handlers):
        logger.removeHandler(h)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(log_level)
    ch.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    logger.addHandler(ch)

    # Optional file handler via LOG_FILE (with rotation)
    log_file = os.getenv("LOG_FILE")
    if log_file:
        fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        fh.setLevel(log_level)
        fh.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
        logger.addHandler(fh)

    # Named app logger
    app_logger = logging.getLogger("camera-auth")
    app_logger.setLevel(log_level)
    return app_logger

log = _configure_logging()

# =========================
# Utilities
# =========================
def _redact(value: Optional[str], keep_tail: int = 4) -> str:
    """Light redaction for secrets in logs."""
    if not value:
        return ""
    if len(value) <= keep_tail:
        return "***"
    return "***" + value[-keep_tail:]

def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 2)

# --- Forwarded Headers Utilities ---
def _build_outbound_forward_headers(req: Request, base_headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    base = dict(base_headers or {})
    inbound_xff = req.headers.get("x-forwarded-for", "")
    x_real_ip_in = req.headers.get("x-real-ip", "")

    client_ip = (req.client.host if getattr(req, "client", None) else None)
    append_ip = client_ip or x_real_ip_in or None

    if inbound_xff and append_ip:
        outbound_xff = f"{inbound_xff}, {append_ip}"
    elif inbound_xff:
        outbound_xff = inbound_xff
    elif append_ip:
        outbound_xff = append_ip
    else:
        outbound_xff = ""

    if outbound_xff:
        base["X-Forwarded-For"] = outbound_xff
    if append_ip:
        base["X-Real-IP"] = append_ip

    return base

# =========================
# Configuration
# =========================
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
EXTERNAL_AUTH_URL = os.environ["EXTERNAL_AUTH_URL"].strip()
KONG_INTERNAL_BASE = os.environ["KONG_INTERNAL_OAUTH_URL"].strip()
KONG_ADMIN_URL = os.environ.get("KONG_ADMIN_URL")
KONG_ADMIN_API_KEY = os.environ.get("KONG_ADMIN_API_KEY")
ISSUE_JWT_URL = os.environ.get("ISSUE_JWT_URL")

# --- Redis Configuration ---
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_TTL = int(os.getenv("REDIS_TTL", 300))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", None)

# --- Redis Client ---
_redis_client = None
def get_redis_client():
    global _redis_client
    if _redis_client is None:
        try:
            log.info(f"Connecting to Redis at {REDIS_HOST}:{REDIS_PORT}")
            _redis_client = redis.StrictRedis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                password=REDIS_PASSWORD,
                decode_responses=True,
                socket_connect_timeout=2
            )
            _redis_client.ping()
            log.info("Successfully connected to Redis.")
        except Exception as e:
            log.error(f"Could not connect to Redis: {e}", exc_info=True)
            _redis_client = None
            raise
    return _redis_client

# =========================
# Helper Functions
# =========================
def generate_auth_code(login_hint: str) -> str:
    timestamp = str(int(time.time()))
    internal_token = f"{login_hint}:{timestamp}"
    raw_data = f"token={internal_token}&login_hint={login_hint}"
    auth_code = base64.urlsafe_b64encode(raw_data.encode()).decode()
    log.debug(f"Generated auth code (payload redacted length={len(raw_data)}).")
    return auth_code

def get_consumer_details_from_kong(client_id: str) -> Tuple[str, str] | None:
    """Fetches the client_secret and consumer username from Kong's Admin API."""
    if not KONG_ADMIN_URL:
        log.error("KONG_ADMIN_URL is not configured. Cannot fetch client credentials.")
        return None

    oauth2_url = f"{KONG_ADMIN_URL.rstrip('/')}/oauth2"
    params = {"client_id": client_id}

    try:
        t0 = time.perf_counter()
        log.info(f"Querying Kong Admin API for OAuth2 credential (client_id={client_id}).")
        log.debug(f"GET {oauth2_url} params={params}")
        oauth_resp = requests.get(oauth2_url, params=params, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        log.debug(f"Kong oauth2 lookup completed in {_duration_ms(t0)} ms with status={oauth_resp.status_code}")
        oauth_resp.raise_for_status()

        oauth_data = oauth_resp.json()
        all_credentials = oauth_data.get("data")

        if not all_credentials: # <-- CHANGED: Check if the 'data' list exists and is not empty
            log.warning(f"No OAuth2 credential found for client_id: {client_id}")
            return None

        # <-- ADDED: Find the credential with the exact matching client_id
        credential = None
        for cred in all_credentials:
            if cred.get("client_id") == client_id:
                credential = cred
                break  # Found the exact match, stop searching

        if not credential:
            log.warning(f"API returned credentials, but none had an exact match for client_id: {client_id}")
            return None
        # <-- END ADDED SECTION

        app_name = credential.get("name", "N/A") # <-- ADDED: Get the application name
        client_secret = credential.get("client_secret")
        consumer_id = credential.get("consumer", {}).get("id")

        if not client_secret or not consumer_id:
            log.error(f"Incomplete OAuth2 credential data for client_id: {client_id} (app_name: {app_name})")
            return None

        log.info(f"Found OAuth2 app '{app_name}' for client_id={client_id}") # <-- ADDED: Improved logging
        log.debug(f"Obtained client_secret={_redact(client_secret)} consumer_id={consumer_id}")

        # Second call to get consumer username from its ID
        consumer_url = f"{KONG_ADMIN_URL.rstrip('/')}/consumers/{consumer_id}"
        t1 = time.perf_counter()
        log.debug(f"GET {consumer_url}")
        consumer_resp = requests.get(consumer_url, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        log.debug(f"Kong consumer lookup completed in {_duration_ms(t1)} ms with status={consumer_resp.status_code}")
        consumer_resp.raise_for_status()

        consumer_data = consumer_resp.json()
        consumer_username = consumer_data.get("username")

        if not consumer_username:
            log.error(f"Could not find username for consumer_id: {consumer_id}")
            return None

        log.info(f"Resolved consumer_username={consumer_username} for client_id={client_id}")
        return client_secret, consumer_username

    except requests.RequestException as e:
        log.error(f"Error calling Kong Admin API: {e}")
        return None

def store_auth_code(auth_code: str, msisdn: str, provision_key: str, client_id: str, client_secret: str, consumer_username: str) -> bool:
    try:
        redis_client = get_redis_client()
        redis_key = f"auth_code:{auth_code}"
        redis_value = json.dumps({
            "msisdn": msisdn,
            "provision_key": provision_key,
            "client_id": client_id,
            "client_secret": client_secret,
            "consumer_username": consumer_username
        })
        log.debug(f"Redis SETEX key={redis_key} ttl={REDIS_TTL} value.redacted="
                  f"{{'msisdn': '{msisdn}', 'provision_key': '***', 'client_id': '{client_id}', "
                  f"'client_secret': '{_redact(client_secret)}', 'consumer_username': '{consumer_username}'}}")
        redis_client.setex(redis_key, REDIS_TTL, redis_value)
        log.info(f"Stored auth context in Redis for consumer={consumer_username}")
        return True
    except redis.RedisError as e:
        log.error(f"Redis error while storing auth code: {e}")
        return False

def validate_and_get_data_from_code(auth_code: str) -> dict | None:
    if not auth_code:
        return None
    try:
        redis_client = get_redis_client()
        redis_key = f"auth_code:{auth_code}"
        stored_data_str = redis_client.get(redis_key)
        if stored_data_str:
            log.debug(f"Redis GET hit for key={redis_key}; deleting key to prevent reuse")
            redis_client.delete(redis_key)
            return json.loads(stored_data_str)
        else:
            log.warning(f"Auth code not found in Redis: {auth_code}")
            return None
    except (redis.RedisError, json.JSONDecodeError) as e:
        log.error(f"Error validating code: {e}")
        return None

# =========================
# FastAPI App + Middleware
# =========================
app = FastAPI(
    title="Camera Authorizer Service",
    description="Securely orchestrates external authentication and token exchange.",
    version="1.7.3"
)
# Paths to skip in access logs (health/readiness/metrics)

SKIP_ACCESS_LOG_PATHS = {"/healthz"}

@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    # Skip noisy endpoints lke healthcheck
    if request.url.path in SKIP_ACCESS_LOG_PATHS:
        return await call_next(request)

    start = time.perf_counter()
    path = request.url.path
    method = request.method
    x_corr = request.headers.get("x-correlator", "-")
    client_ip = request.client.host if request.client else "-"
    log.info(f">>> {method} {path} corr={x_corr} ip={client_ip}")

    # DEBUG: headers and (for POST form) body snapshot
    if log.isEnabledFor(logging.DEBUG):
        try:
            # Peek form if small; avoid consuming for big payloads
            if method in ("POST", "PUT", "PATCH"):
                # read body safely
                body = await request.body()
                preview = body[:1024]
                log.debug(f"Request headers: {dict(request.headers)}")
                log.debug(f"Request body (first 1KB): {preview!r}")
                # Reconstruct request stream for downstream
                async def receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                request = Request(request.scope, receive=receive)
            else:
                log.debug(f"Request headers: {dict(request.headers)}")
        except Exception as e:
            log.debug(f"Could not log request body: {e}")

    try:
        response = await call_next(request)
        dur = _duration_ms(start)
        log.info(f"<<< {method} {path} status={response.status_code} dur_ms={dur} corr={x_corr}")
        return response
    except Exception as e:
        dur = _duration_ms(start)
        log.exception(f"xxx {method} {path} failed dur_ms={dur} corr={x_corr}")
        raise

# =========================
# Endpoints
# =========================
@app.get("/healthz")
def healthz():
    try:
        get_redis_client().ping()
        return {"status": "ok", "redis_connection": "ok"}
    except Exception as e:
        log.error(f"Health check failed: {e}")
        raise HTTPException(status_code=503, detail=f"Service is unhealthy: {str(e)}")

@app.post("/authorizer")
def handle_authorization(
    request: Request,
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    provision_key: str = Form(...),
    response_type: str = Form(...),
    scope: str = Form(None),
    authenticated_userid: str = Form(...),
    ipAddress: Optional[str] = Form(None),  # optional
    grant_type: str = Form(None),
    x_correlator: str | None = Header(default=None, alias="x-correlator"),
):
    t0 = time.perf_counter()
    try:
        log.info(
            f"Authorization request for client_id={client_id}, "
            f"ipAddress={ipAddress}, response_type={response_type}, grant_type={grant_type}"
        )
        log.debug(f"Form scope={scope}, authenticated_userid={authenticated_userid}, provision_key=***")

        # Sanity checks
        if response_type != "code":
            raise HTTPException(status_code=400, detail="response_type must be 'code'")
        if grant_type and grant_type != "authorization_code":
            raise HTTPException(status_code=400, detail="grant_type must be 'authorization_code' when provided")

        # (1) External auth call (only ipAddress is sent if present)
        ext_headers = _build_outbound_forward_headers(request, {"x-correlator": x_correlator} if x_correlator else {})
        ext_params = {}
        if ipAddress:
            ext_params["ipAddress"] = ipAddress

        log.info(f"Calling EXTERNAL_AUTH_URL={EXTERNAL_AUTH_URL}")
        log.debug(f"External auth params={ext_params} headers={ext_headers}")

        t_ext = time.perf_counter()
        external_resp = requests.get(
            EXTERNAL_AUTH_URL,
            params=ext_params if ext_params else None,
            headers=ext_headers,
            timeout=REQ_TIMEOUT,
            verify=VERIFY_TLS
        )
        log.debug(f"External auth completed in {_duration_ms(t_ext)} ms status={external_resp.status_code}")

        if external_resp.status_code != 200:
            log.warning(f"External auth failed [{external_resp.status_code}]: {external_resp.text[:512]}")
            try:
                error_detail = external_resp.json()
            except requests.exceptions.JSONDecodeError:
                error_detail = {"detail": external_resp.text or "Unknown error"}
            raise HTTPException(
                status_code=external_resp.status_code,
                detail=error_detail.get("detail", error_detail)
            )

        try:
            auth_json = external_resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("External auth returned non-JSON body")
            raise HTTPException(status_code=502, detail="External auth returned invalid JSON")

        msisdn_from_ext = auth_json.get("msisdn")
        match = auth_json.get("match", False)

        if not match:
            log.info(f"External auth not matched for ipAddress={ipAddress}: {auth_json}")
            raise HTTPException(status_code=401, detail="Authentication not matched")

        if not msisdn_from_ext or not isinstance(msisdn_from_ext, str):
            log.error(f"External auth JSON missing/invalid msisdn: {auth_json}")
            raise HTTPException(status_code=502, detail="External auth response missing msisdn")

        log.info(f"External authentication successful. msisdn={msisdn_from_ext}")

        # (2) Fetch client_secret + consumer_username from Kong Admin
        consumer_details = get_consumer_details_from_kong(client_id)
        if not consumer_details:
            raise HTTPException(status_code=401, detail="Invalid client_id or client credentials not found.")
        client_secret, consumer_username = consumer_details
        log.debug(f"Using client_id={client_id} client_secret={_redact(client_secret)} consumer={consumer_username}")

        # (3) Generate auth code using msisdn
        custom_auth_code = generate_auth_code(msisdn_from_ext)

        # (4) Store context in Redis
        if not store_auth_code(
            custom_auth_code,
            msisdn_from_ext,
            provision_key,
            client_id,
            client_secret,
            consumer_username
        ):
            raise HTTPException(status_code=503, detail="Failed to store authorization context. Please try again later.")

        # (5) Return redirect with the custom code
        final_redirect_uri = f"{redirect_uri}?code={custom_auth_code}"
        log.info(f"Issued auth code for consumer={consumer_username}. Redirecting to: {final_redirect_uri}")
        log.debug(f"Total /authorizer duration: {_duration_ms(t0)} ms")
        return JSONResponse(content={"redirect_uri": final_redirect_uri})

    except HTTPException:
        log.debug(f"/authorizer failed after {_duration_ms(t0)} ms")
        raise
    except Exception as e:
        log.exception("Unexpected error during authorization")
        raise HTTPException(status_code=500, detail="An internal server error occurred")

@app.post("/token")
def handle_custom_token_exchange(code: str = Header(...)):
    if not ISSUE_JWT_URL:
        log.error("FATAL: ISSUE_JWT_URL environment variable is not set.")
        raise HTTPException(status_code=500, detail="Service is not configured correctly.")

    t0 = time.perf_counter()
    try:
        log.info("Token exchange started.")
        log.debug(f"Incoming code header present={bool(code)}")

        auth_data = validate_and_get_data_from_code(code)
        if not auth_data:
            raise HTTPException(status_code=400, detail="Invalid, expired, or previously used code.")

        # Retrieve data from Redis
        client_id = auth_data.get("client_id")
        client_secret = auth_data.get("client_secret")
        authenticated_msisdn = auth_data.get("msisdn")
        consumer_username = auth_data.get("consumer_username")

        if not all([client_id, client_secret, authenticated_msisdn, consumer_username]):
            log.error("Stored authorization context is incomplete.")
            raise HTTPException(status_code=500, detail="Stored authorization context is incomplete.")

        log.info(f"Auth code validated for consumer={consumer_username}")
        log.debug(f"auth_data.redacted={{'client_id': '{client_id}', 'client_secret': '{_redact(client_secret)}', 'msisdn': '{authenticated_msisdn}', 'consumer_username': '{consumer_username}'}}")

        # (1) Intermediate token from Kong
        kong_token_url = KONG_INTERNAL_BASE.rstrip("/") + "/oauth2/token"
        kong_payload = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret
        }
        log.info("Requesting intermediate access token from Kong.")
        log.debug(f"POST {kong_token_url} payload.redacted={{'grant_type':'client_credentials','client_id':'{client_id}','client_secret':'{_redact(client_secret)}'}}")

        t_kong = time.perf_counter()
        kong_resp = requests.post(kong_token_url, data=kong_payload, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        log.debug(f"Kong token call completed in {_duration_ms(t_kong)} ms status={kong_resp.status_code}")

        if kong_resp.status_code != 200:
            body_preview = kong_resp.text[:512]
            log.error(f"Failed to get intermediate token from Kong. status={kong_resp.status_code} body={body_preview}")
            try:
                response_content = kong_resp.json()
            except requests.exceptions.JSONDecodeError:
                response_content = body_preview
            raise HTTPException(status_code=502, detail={"error": "Failed to get intermediate token", "upstream_response": response_content})

        access_token = kong_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Intermediate token request succeeded but no access_token was found.")

        log.info("Retrieved intermediate access token from Kong.")

        # (2) Final JWT issuer call
        headers = {
            'Authorization': f'Bearer {access_token}',
            'X-Consumer-Username': consumer_username,
            'X-Login-Hint': authenticated_msisdn
        }
        log.info(f"Calling final JWT issuer at {ISSUE_JWT_URL} for consumer={consumer_username}")
        log.debug(f"JWT issuer headers.redacted={{'Authorization':'Bearer ***','X-Consumer-Username':'{consumer_username}','X-Login-Hint':'{authenticated_msisdn}'}}")

        t_jwt = time.perf_counter()
        final_jwt_resp = requests.get(ISSUE_JWT_URL, headers=headers, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        log.debug(f"JWT issuer call completed in {_duration_ms(t_jwt)} ms status={final_jwt_resp.status_code}")

        try:
            final_response_content = final_jwt_resp.json()
        except requests.exceptions.JSONDecodeError:
            final_response_content = final_jwt_resp.text

        log.info(f"Token exchange finished with status={final_jwt_resp.status_code} total_ms={_duration_ms(t0)}")
        return JSONResponse(status_code=final_jwt_resp.status_code, content=final_response_content)

    except HTTPException:
        log.debug(f"/token failed after {_duration_ms(t0)} ms")
        raise
    except requests.RequestException as e:
        log.exception("HTTP request error during token exchange orchestration")
        raise HTTPException(status_code=503, detail=f"A downstream service is unavailable: {e}")
    except Exception as e:
        log.exception("Unexpected error during token exchange")
        raise HTTPException(status_code=500, detail="An internal server error occurred")
