#!/usr/bin/env python3
import os
import logging
import time
import base64
import json
import hashlib
import hmac
from logging.handlers import RotatingFileHandler
from urllib.parse import urlparse, parse_qs, urlencode
from typing import Dict, Any, Tuple, Optional
import secrets

import requests
import redis
from fastapi import FastAPI, Response, Form, HTTPException, Header, Request, Query
from fastapi.responses import JSONResponse, RedirectResponse

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
REFRESH_TTL_SECONDS = int(os.getenv("REFRESH_TTL_SECONDS", 86400))  
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
        log.debug(f"Querying Kong Admin API for OAuth2 credential (client_id={client_id}).")
        log.debug(f"GET {oauth2_url} params={params}")
        oauth_resp = requests.get(oauth2_url, params=params, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        log.debug(f"Kong oauth2 lookup completed in {_duration_ms(t0)} ms with status={oauth_resp.status_code}")
        oauth_resp.raise_for_status()

        oauth_data = oauth_resp.json()
        all_credentials = oauth_data.get("data")

        if not all_credentials: 
            log.warning(f"No OAuth2 credential found for client_id: {client_id}")
            return None

        # Find the credential with the exact matching client_id
        credential = None
        for cred in all_credentials:
            if cred.get("client_id") == client_id:
                credential = cred
                break  # Found the exact match, stop searching

        if not credential:
            log.warning(f"API returned credentials, but none had an exact match for client_id: {client_id}")
            return None

        app_name = credential.get("name", "N/A")
        client_secret = credential.get("client_secret")
        consumer_id = credential.get("consumer", {}).get("id")

        if not client_secret or not consumer_id:
            log.error(f"Incomplete OAuth2 credential data for client_id: {client_id} (app_name: {app_name})")
            return None

        log.debug(f"Found OAuth2 app '{app_name}' for client_id={client_id}")
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

        log.debug(f"Resolved consumer_username={consumer_username} for client_id={client_id}")
        return client_secret, consumer_username

    except requests.RequestException as e:
        log.error(f"Error calling Kong Admin API: {e}")
        return None

def store_auth_code(
    auth_code: str, 
    msisdn: str, 
    provision_key: Optional[str], 
    client_id: str, 
    client_secret: str, 
    consumer_username: str,
    scope: Optional[str] = None,
    api_version: str = "v1"  # New parameter to differentiate v1 and v2
) -> bool:
    try:
        redis_client = get_redis_client()
        redis_key = f"auth_code:{auth_code}"
        
        # Prepare the value with version information
        data_to_store = {
            "msisdn": msisdn,
            "provision_key": provision_key if provision_key else "",
            "client_id": client_id,
            "client_secret": client_secret,
            "consumer_username": consumer_username,
            "scope": scope if scope else "",
            "api_version": api_version
        }
        
        redis_value = json.dumps(data_to_store)
        
        log.debug(f"Redis SETEX key={redis_key} ttl={REDIS_TTL} value.redacted="
                  f"{{'msisdn': '{msisdn}', 'api_version': '{api_version}', 'client_id': '{client_id}', "
                  f"'client_secret': '{_redact(client_secret)}', 'consumer_username': '{consumer_username}'}}")
        
        redis_client.setex(redis_key, REDIS_TTL, redis_value)
        log.debug(f"Stored auth context ({api_version}) in Redis for consumer={consumer_username}")
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

def _extract_state_from_request_object(request_jwt: str) -> Optional[str]:
    """
    gsma  is sending state in request object,so Best-effort extraction of `state` from a JWT-style `request` object.
    """
    try:
        parts = request_jwt.split(".")
        if len(parts) != 3:
            log.warning("request object is not a 3-part JWT; cannot extract state")
            return None

        payload_b64 = parts[1]
        # Add padding if needed
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))

        state_val = payload.get("state")
        log.debug(f"Extracted state from request object: {state_val!r}")
        return state_val
    except Exception as e:
        log.warning(f"Failed to extract state from request object: {e}")
        return None


def _extract_scope_from_request_object(request_jwt: str) -> Optional[str]:
    """
    Best-effort extraction of `scope` from a JWT-style `request` object.
    Some environments put OAuth parameters only in the request object.
    """
    try:
        parts = request_jwt.split(".")
        if len(parts) != 3:
            log.warning("request object is not a 3-part JWT; cannot extract scope")
            return None

        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + padding)
        payload = json.loads(payload_bytes.decode("utf-8"))

        scope = payload.get("scope")
        if isinstance(scope, str) and scope.strip():
            return scope.strip()
        return None
    except Exception as e:
        log.warning(f"Failed to extract scope from request object: {e}")
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def generate_id_token(
    *,
    msisdn: str,
    client_id: str,
    signing_secret: str,
    expires_in: int = 3600,
    issuer: str = "camera-auth",
) -> str:
    """
    Generates a minimal HS256 ID Token.
    NOTE: This is intentionally basic; add standard OIDC claims as needed.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": issuer,
        "sub": msisdn,
        "aud": client_id,
        "iat": now,
        "exp": now + int(expires_in),
    }

    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{payload_b64}"
    sig = hmac.new(
        signing_secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return f"{signing_input}.{_b64url(sig)}"


def _require(d: dict, key: str) -> str:
    val = d.get(key)
    if not val:
        raise HTTPException(status_code=400, detail=f"Missing '{key}' in auth_code context")
    return val

def issue_jwt_token(
    *,
    msisdn: str,
    client_id: str,
    client_secret: str,
    consumer_username: str,
    api_version: str = "v2",
    x_correlator: Optional[str] = None,
    scope: Optional[str] = None,
) -> dict:
    if not KONG_INTERNAL_BASE:
        raise HTTPException(status_code=500, detail="KONG_INTERNAL_OAUTH_URL is not configured.")
    if not ISSUE_JWT_URL:
        raise HTTPException(status_code=500, detail="ISSUE_JWT_URL is not configured.")

    # (1) Intermediate token from Kong (client_credentials)
    kong_token_url = KONG_INTERNAL_BASE.rstrip("/") + "/oauth2/token"
    kong_payload = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }

    t_kong = time.perf_counter()
    kong_resp = requests.post(
        kong_token_url,
        data=kong_payload,
        timeout=REQ_TIMEOUT,
        verify=VERIFY_TLS,
    )
    log.debug(f"Kong token call completed in {_duration_ms(t_kong)} ms status={kong_resp.status_code} response={kong_resp}")

    if kong_resp.status_code != 200:
        body_preview = kong_resp.text[:512]
        try:
            response_content = kong_resp.json()
        except Exception:
            response_content = body_preview
        raise HTTPException(
            status_code=502,
            detail={
                "error": "failed_to_get_intermediate_token",
                "upstream_status": kong_resp.status_code,
                "upstream_response": response_content,
            },
        )

    try:
        access_token = kong_resp.json().get("access_token")
    except Exception:
        access_token = None

    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Intermediate token request succeeded but no access_token was found.",
        )

    # (2) Final JWT issuer call
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Consumer-Username": consumer_username,
        "X-Login-Hint": msisdn,
    }
    if scope and scope.strip():
        headers["scope"] = scope.strip()
        
    if x_correlator:
        headers["x-correlator"] = x_correlator

    t_jwt = time.perf_counter()
    final_resp = requests.get(
        ISSUE_JWT_URL,
        headers=headers,
        timeout=REQ_TIMEOUT,
        verify=VERIFY_TLS,
    )
    log.debug(f"JWT issuer call completed in {_duration_ms(t_jwt)} ms status={final_resp.status_code} response from JWT Issuer={final_resp}")

    try:
        content = final_resp.json()
    except Exception:
        content = {"raw": final_resp.text}

    if final_resp.status_code >= 400:
        raise HTTPException(
            status_code=final_resp.status_code,
            detail={"error": "jwt_issuer_error", "upstream_response": content},
        )

    if not isinstance(content, dict):
        content = {"result": content}
    return content

def _extract_access_token_from_jwt_response(jwt_response: Any) -> Optional[str]:
    if jwt_response is None:
        return None

    if isinstance(jwt_response, str):
        return jwt_response.strip() or None

    if not isinstance(jwt_response, dict):
        try:
            return str(jwt_response)
        except Exception:
            return None
    # Common field names across implementations
    for k in (
        "access_token",
        "token",
        "jwt",
        "id_token",  # some issuers return the JWT here
        "result",
    ):
        v = jwt_response.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    # Some issuers wrap the token: { data: { access_token: ... } }
    data = jwt_response.get("data")
    if isinstance(data, dict):
        for k in ("access_token", "token", "jwt", "id_token"):
            v = data.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    # Some issuers return: { raw: "<jwt>" }
    raw = jwt_response.get("raw")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None

def store_refresh_token(redis, refresh_token: str, data: dict) -> None:
    # Store only what you need to validate/refresh later
    payload = {
        "msisdn": data.get("msisdn"),
        "provision_key": data.get("provision_key", ""),
        "client_id": data.get("client_id"),
        "client_secret": data.get("client_secret"), 
        "consumer_username": data.get("consumer_username"),
        "scope": data.get("scope", ""),
        "api_version": data.get("api_version", "v2"),
        "issued_at": int(time.time()),
    }
    redis.setex(
        f"refresh_token:{refresh_token}",
        REFRESH_TTL_SECONDS,
        json.dumps(payload),
    )
    
def generate_refresh_token() -> str:
    # 64 chars
    return secrets.token_urlsafe(48)


app = FastAPI(
    title="Camera Authorizer Service",
    description="Securely orchestrates external authentication and token exchange.",
    version="1.8.0"
)
# Paths to skip in access logs (health/readiness/metrics)

SKIP_ACCESS_LOG_PATHS = {"/healthz"}

@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    # Skip noisy endpoints like healthcheck
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

# V2 Authorization Endpoint (GET with Redirect)
@app.get("/v2/authorize")
def handle_authorization_v2(
    request: Request,
    response_type: str = Query(..., description="Must be 'code'"),
    redirect_uri: str = Query(...),
    client_id: str = Query(...),
    scope: Optional[str] = Query(None),
    request_jwt: Optional[str] = Query(None, alias="request"),
    state: Optional[str] = Query(None),
    prompt: str = Query("none"),
    ipAddress: Optional[str] = Query(None),                
    x_correlator: Optional[str] = Header(default=None, alias="x-correlator"),
):
    t0 = time.perf_counter()
    
    try:
        raw_url = str(request.url)
        method = request.method
        client_host = request.client.host if request.client else "unknown"

        # Convert headers to dict and sanitize
        incoming_headers = dict(request.headers)
        incoming_headers.pop("authorization", None)
        incoming_headers.pop("cookie", None)

        log.debug(f"Incoming /v2/authorize request:")
        log.debug(f"Method: {method}")
        log.debug(f"URL: {raw_url}")
        log.debug(f"client IP: {client_host}")
        log.debug(f"Query Params: {dict(request.query_params)}")
        log.debug(f"Headers: {incoming_headers}")

    except Exception as log_err:
        log.warning(f"Failed to log incoming request: {log_err}")
        
    try:
        # --- Resolve IP (prefer explicit ipAddress, then headers, then socket) ---
        xff = request.headers.get("x-forwarded-for", "")
        x_real = request.headers.get("x-real-ip", "")

        if ipAddress:
            resolved_ip = ipAddress
        elif xff:
            resolved_ip = xff.split(",")[0].strip()
        elif x_real:
            resolved_ip = x_real
        elif request.client:
            resolved_ip = request.client.host
        else:
            resolved_ip = None

        ipAddress = resolved_ip

        log.debug(
            f"V2 Authorization request for client_id={client_id}, "
            f"ipAddress={ipAddress}, response_type={response_type}"
        )
        log.debug(

            f"Query scope={scope}, state={state}, prompt={prompt}, "
            f"has_request_jwt={bool(request_jwt)}"

        )

        if response_type != "code":
            raise HTTPException(status_code=400, detail="Unsupported response_type. Only 'code' is allowed.")

        # adding basic   validation on redirect_uri 
        parsed_redirect = urlparse(redirect_uri)
        if not parsed_redirect.scheme or not parsed_redirect.netloc:
            raise HTTPException(status_code=400, detail="Invalid redirect_uri")
        
        # --- External auth call ---
        extra_headers = {"x-correlator": x_correlator} if x_correlator else {}
        ext_headers = _build_outbound_forward_headers(request, extra_headers)

        ext_params = {}
        if ipAddress:
            ext_params["ipAddress"] = ipAddress

        log.debug(f"V2: Calling EXTERNAL_AUTH_URL={EXTERNAL_AUTH_URL}")
        log.debug(f"V2 External auth params={ext_params} headers={ext_headers}")

        t_ext = time.perf_counter()
        external_resp = requests.get(
            EXTERNAL_AUTH_URL,
            params=ext_params if ext_params else None,
            headers=ext_headers,
            timeout=REQ_TIMEOUT,
            verify=VERIFY_TLS,
        )
        log.debug(
            f"External auth completed in {_duration_ms(t_ext)} ms "
            f"status={external_resp.status_code}"
        )

        if external_resp.status_code != 200:
            log.warning(
                f"External auth failed [{external_resp.status_code}]: "
                f"{external_resp.text[:512]}"
            )
            try:
                error_detail = external_resp.json()
            except requests.exceptions.JSONDecodeError:
                error_detail = {"detail": external_resp.text or "Unknown error"}
            raise HTTPException(
                status_code=external_resp.status_code,
                detail=error_detail.get("detail", error_detail),
            )

        try:
            auth_json = external_resp.json()
        except requests.exceptions.JSONDecodeError:
            log.error("External auth returned non-JSON body")
            raise HTTPException(status_code=502, detail="External auth invalid response")

        msisdn_from_ext = auth_json.get("msisdn")
        match = auth_json.get("match", False)

        if not match:
            log.debug(f"External auth not matched for ipAddress={ipAddress}: {auth_json}")
            raise HTTPException(status_code=401, detail="Authentication not matched")

        if not msisdn_from_ext or not isinstance(msisdn_from_ext, str):
            log.error(f"External auth JSON missing/invalid msisdn: {auth_json}")
            raise HTTPException(status_code=502, detail="External auth response missing msisdn")

        log.debug(f"External authentication successful. msisdn={msisdn_from_ext}")

        # --- Fetch client_secret + consumer_username from Kong Admin ---
        consumer_details = get_consumer_details_from_kong(client_id)
        if not consumer_details:
            raise HTTPException(status_code=401, detail="Invalid client_id or client credentials not found.")
        client_secret, consumer_username = consumer_details
        log.debug(f"Using client_id={client_id} client_secret={_redact(client_secret)} consumer={consumer_username}")

        # --- Generate and store auth code as V2 ---
        custom_auth_code = generate_auth_code(msisdn_from_ext)

        effective_scope = scope
        if not effective_scope and request_jwt:
            effective_scope = _extract_scope_from_request_object(request_jwt)

        if not store_auth_code(
            custom_auth_code,
            msisdn_from_ext,
            None,                  
            client_id,
            client_secret,
            consumer_username,
            scope=effective_scope,
            api_version="v2",
        ):
            raise HTTPException(status_code=500, detail="Failed to store auth code")

        # --- Build redirect URL ---
        effective_state = state
        if not effective_state and request_jwt:
            effective_state = _extract_state_from_request_object(request_jwt)
        log.debug(
            f"V2: received state={state!r}, "
            f"derived state from request={_extract_state_from_request_object(request_jwt) if request_jwt else None!r}, "
            f"using effective_state={effective_state!r}"
        )
        query_params = {"code": custom_auth_code}
        if effective_state:
            query_params["state"] = effective_state
        separator = "&" if "?" in redirect_uri else "?"
        final_redirect_uri = f"{redirect_uri}{separator}{urlencode(query_params)}"

        log.debug(
            f"V2: Issued auth code for consumer={consumer_username}. "
            f"Redirecting to: {final_redirect_uri}"
        )
        log.debug(f"Total /v2/authorize duration: {_duration_ms(t0)} ms")

        return RedirectResponse(url=final_redirect_uri, status_code=302)

    except HTTPException:
        log.debug(f"/v2/authorize failed after {_duration_ms(t0)} ms")
        raise
    except Exception:
        log.exception("Unexpected error during v2 authorization")
        raise HTTPException(status_code=500, detail="An internal server error occurred")

# V1 Authorization Endpoint (POST)
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

        # (4) Store context in Redis (Mark as V1)
        if not store_auth_code(
            custom_auth_code,
            msisdn_from_ext,
            provision_key,
            client_id,
            client_secret,
            consumer_username,
            api_version="v1"
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

@app.post("/v2/token")
def token_exchange_v2(
    grant_type: str = Form(...),
    code: Optional[str] = Form(default=None),
    redirect_uri: Optional[str] = Form(default=None),
    client_assertion_type: Optional[str] = Form(default=None),
    client_assertion: Optional[str] = Form(default=None),
    refresh_token: Optional[str] = Form(default=None),
    x_correlator: Optional[str] = Header(default=None, alias="x-correlator"),
):
    t0 = time.time()
    redis_client = get_redis_client()

    log.info(f"/v2/token called with grant_type={grant_type}")
    log.debug(f"x-correlator={x_correlator}")

    try:
        if grant_type == "refresh_token":
            log.info("Processing refresh_token grant")

            if not refresh_token:
                log.warning("Missing refresh_token in request")
                raise HTTPException(status_code=400, detail="Missing refresh_token")

            rt_key = f"refresh_token:{refresh_token}"
            log.debug(f"Looking up Redis key={rt_key}")

            rt_json = redis_client.get(rt_key)
            if not rt_json:
                log.warning("Refresh token not found or expired")
                raise HTTPException(status_code=400, detail="Invalid or expired refresh_token")

            rt_data = json.loads(rt_json)
            log.debug(f"Refresh token payload loaded: {rt_data}")

            msisdn = rt_data.get("msisdn")
            client_id = rt_data.get("client_id")
            client_secret = rt_data.get("client_secret")
            consumer_username = rt_data.get("consumer_username")
            api_version = rt_data.get("api_version", "v2")

            log.info(
                f"Refreshing access token for client_id={client_id}, msisdn={msisdn}"
            )

            #rtate refresh token
            redis_client.delete(rt_key)
            log.info("Old refresh token deleted (rotation)")
            log.debug(f"Deleted Redis key={rt_key}")

            new_refresh_token = generate_refresh_token()
            store_refresh_token(redis_client, new_refresh_token, rt_data)

            #issue new access token
            log.info("Issuing new access token via JWT issuer")

            jwt_response = issue_jwt_token(
                msisdn=msisdn,
                client_id=client_id,
                client_secret=client_secret,
                consumer_username=consumer_username,
                api_version=api_version,
                scope=scope_str, 
                x_correlator=x_correlator,
            )

            new_access_token = _extract_access_token_from_jwt_response(jwt_response)
            log.debug(f"JWT issuer raw response={jwt_response}")

            scope_str = (rt_data.get("scope") or "").strip()
            include_id_token = "openid" in scope_str.split()

            response_data = {
                "access_token": new_access_token,
                "refresh_token": new_refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
                "msisdn": msisdn,
                "api_version": api_version,
            }

            if include_id_token:
                signing_secret = client_secret or os.getenv("ID_TOKEN_SIGNING_SECRET", "dev-id-token-secret")
                response_data["id_token"] = generate_id_token(
                    msisdn=msisdn,
                    client_id=client_id,
                    signing_secret=signing_secret,
                    expires_in=3600,
                    issuer="camera-auth",
                )

            log.info("Refresh token grant completed successfully")
            log.debug(f"/v2/token(refresh) duration={_duration_ms(t0)} ms")

            return JSONResponse(content=response_data)

        log.info("Processing authorization_code grant")

        if grant_type != "authorization_code":
            log.warning(f"Unsupported grant_type={grant_type}")
            raise HTTPException(status_code=400, detail="Unsupported grant_type")

        if not code:
            log.warning("Authorization code missing in request")
            raise HTTPException(status_code=400, detail="Missing authorization code")

        auth_key = f"auth_code:{code}"
        log.debug(f"Fetching Redis key={auth_key}")

        auth_json = redis_client.get(auth_key)
        if not auth_json:
            log.warning("Authorization code invalid or expired")
            raise HTTPException(status_code=400, detail="Invalid authorization code")

        auth_data = json.loads(auth_json)
        log.info("Authorization code validated successfully")
        log.debug(f"Authorization payload={auth_data}")

        #delete auth code (single-use)
        redis_client.delete(auth_key)
        log.info("Authorization code deleted (single-use enforced)")
        log.debug(f"Deleted Redis key={auth_key}")

        msisdn = auth_data["msisdn"]
        client_id = auth_data["client_id"]
        client_secret = auth_data["client_secret"]
        consumer_username = auth_data["consumer_username"]
        api_version = auth_data.get("api_version", "v2")
        scope_str = (auth_data.get("scope") or "").strip()

        include_id_token = "openid" in scope_str.split()
        # issuerefresh token
        log.info("Issuing refresh token for new session")
        new_refresh_token = generate_refresh_token()
        store_refresh_token(redis_client, new_refresh_token, auth_data)

        # isse access token
        log.info("Issuing access token via JWT issuer")

        jwt_response = issue_jwt_token(
            msisdn=msisdn,
            client_id=client_id,
            client_secret=client_secret,
            consumer_username=consumer_username,
            api_version=api_version,
            scope=scope_str,  
            x_correlator=x_correlator,
        )

        extracted_token = _extract_access_token_from_jwt_response(jwt_response)
        log.debug(f"JWT issuer raw response={jwt_response}")

        response_data = {
            "access_token": extracted_token,
            "refresh_token": new_refresh_token,
            "token_type": "Bearer",
            "expires_in": 3600,
            "msisdn": msisdn,
            "api_version": api_version,
        }

        if include_id_token:
            # Sign with client_secret so the client can validate it (or override via env).
            signing_secret = client_secret or os.getenv("ID_TOKEN_SIGNING_SECRET", "dev-id-token-secret")
            response_data["id_token"] = generate_id_token(
                msisdn=msisdn,
                client_id=client_id,
                signing_secret=signing_secret,
                expires_in=3600,
                issuer="camera-auth",
            )

        log.info("Authorization_code grant completed successfully")
        log.debug(f"/v2/token(auth_code) duration={_duration_ms(t0)} ms")

        return JSONResponse(content=response_data)

    except HTTPException:
        log.info(f"/v2/token failed after {_duration_ms(t0)} ms")
        raise
    except Exception as e:
        log.exception(f"Unhandled error in /v2/token: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")



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