#!/usr/bin/env python3
import os
import logging
import time
import base64
import json
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Tuple

import requests
import redis
from fastapi import FastAPI, Response, Form, HTTPException, Header
from fastapi.responses import JSONResponse

# --- Configuration and Logging ---
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("camera-auth")
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
            _redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, password=REDIS_PASSWORD, decode_responses=True, socket_connect_timeout=2)
            _redis_client.ping()
            log.info("Successfully connected to Redis.")
        except Exception as e:
            log.error(f"Could not connect to Redis: {e}", exc_info=True)
            _redis_client = None; raise
    return _redis_client


# --- Helper Functions ---

def generate_auth_code(login_hint: str) -> str:
    timestamp = str(int(time.time()))
    internal_token = f"{login_hint}:{timestamp}"
    raw_data = f"token={internal_token}&login_hint={login_hint}"
    auth_code = base64.urlsafe_b64encode(raw_data.encode()).decode()
    log.info(f"Generated auth code. Decoded payload will be: '{raw_data}'")
    return auth_code

# THIS IS THE MODIFIED FUNCTION
def get_consumer_details_from_kong(client_id: str) -> Tuple[str, str] | None:
    """Fetches the client_secret and consumer username from Kong's Admin API."""
    if not KONG_ADMIN_URL:
        log.error("KONG_ADMIN_URL is not configured. Cannot fetch client credentials.")
        return None

    oauth2_url = f"{KONG_ADMIN_URL.rstrip('/')}/oauth2"
    params = {"client_id": client_id}
    
    try:
        log.info(f"Querying Kong Admin API: {oauth2_url} for client_id: {client_id}")
        oauth_resp = requests.get(oauth2_url, params=params, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        oauth_resp.raise_for_status()

        oauth_data = oauth_resp.json()
        if not (oauth_data.get("data") and len(oauth_data["data"]) > 0):
            log.warning(f"No OAuth2 credential found for client_id: {client_id}")
            return None

        credential = oauth_data["data"][0]
        client_secret = credential.get("client_secret")
        consumer_id = credential.get("consumer", {}).get("id")

        if not client_secret or not consumer_id:
            log.error(f"Incomplete OAuth2 credential data for client_id: {client_id}")
            return None
        
        log.info(f"Successfully retrieved client_secret and consumer_id: {consumer_id}")

        # --- NEW: Second call to get consumer username from its ID ---
        consumer_url = f"{KONG_ADMIN_URL.rstrip('/')}/consumers/{consumer_id}"
        log.info(f"Querying Kong Admin API for consumer username: {consumer_url}")
        consumer_resp = requests.get(consumer_url, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        consumer_resp.raise_for_status()
        
        consumer_data = consumer_resp.json()
        consumer_username = consumer_data.get("username")
        
        if not consumer_username:
            log.error(f"Could not find username for consumer_id: {consumer_id}")
            return None
            
        log.info(f"Successfully retrieved consumer_username: {consumer_username}")
        return client_secret, consumer_username

    except requests.RequestException as e:
        log.error(f"Error calling Kong Admin API: {e}"); return None

def store_auth_code(auth_code: str, msisdn: str, provision_key: str, client_id: str, client_secret: str, consumer_username: str) -> bool:
    try:
        redis_client = get_redis_client()
        redis_key = f"auth_code:{auth_code}"
        redis_value = json.dumps({
            "msisdn": msisdn, "provision_key": provision_key, "client_id": client_id,
            "client_secret": client_secret, "consumer_username": consumer_username
        })
        redis_client.setex(redis_key, REDIS_TTL, redis_value)
        log.info(f"Successfully stored auth context in Redis for consumer: {consumer_username}.")
        return True
    except redis.RedisError as e:
        log.error(f"Redis error while storing auth code: {e}"); return False

def validate_and_get_data_from_code(auth_code: str) -> dict | None:
    if not auth_code: return None
    try:
        redis_client = get_redis_client()
        redis_key = f"auth_code:{auth_code}"
        stored_data_str = redis_client.get(redis_key)
        if stored_data_str:
            log.info("Auth code found in Redis. Deleting it to prevent reuse.")
            redis_client.delete(redis_key); return json.loads(stored_data_str)
        else:
            log.warning(f"Auth code not found in Redis: {auth_code}"); return None
    except (redis.RedisError, json.JSONDecodeError) as e:
        log.error(f"Error validating code: {e}"); return None

# --- FastAPI Application ---
app = FastAPI(title="Camera Authorizer Service", description="Securely orchestrates external authentication and token exchange.", version="1.7.2")

@app.get("/healthz")
def healthz():
    try:
        get_redis_client().ping(); return {"status": "ok", "redis_connection": "ok"}
    except Exception as e:
        log.error(f"Health check failed: {e}"); raise HTTPException(status_code=503, detail=f"Service is unhealthy: {str(e)}")

@app.post("/authorizer")
def handle_authorization(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    provision_key: str = Form(...),
    login_hint: str = Form(...),
    response_type: str = Form(...),
    scope: str = Form(None),
    authenticated_userid: str = Form(...),
    identifier: str = Form(...),
    carrierName: str = Form(...),
    customerName: str = Form(...),
    ipAddress: str = Form(...),
    grant_type: str = Form(None)
):
    try:
        log.info(f"Received authorization request for login_hint: {login_hint}, client_id: {client_id}")
        
        # MODIFIED: Fetch both secret and username
        consumer_details = get_consumer_details_from_kong(client_id)
        if not consumer_details:
            raise HTTPException(status_code=401, detail="Invalid client_id or client credentials not found.")
        client_secret, consumer_username = consumer_details

        auth_params = { "identifier": identifier, "carrierName": carrierName, "customerName": customerName, "msisdn": login_hint, "ipAddress": ipAddress }
        external_resp = requests.get(EXTERNAL_AUTH_URL, params=auth_params, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        if external_resp.status_code != 200:
            log.warning(f"External auth failed with status {external_resp.status_code}: {external_resp.text}")
            try: error_detail = external_resp.json()
            except requests.exceptions.JSONDecodeError: error_detail = {"detail": external_resp.text or "Unknown error"}
            raise HTTPException(status_code=external_resp.status_code, detail=error_detail.get("detail", error_detail))
        
        log.info("External authentication successful.")
        custom_auth_code = generate_auth_code(login_hint)

        # MODIFIED: Store the consumer_username in Redis
        if not store_auth_code(custom_auth_code, login_hint, provision_key, client_id, client_secret, consumer_username):
            raise HTTPException(status_code=503, detail="Failed to store authorization context. Please try again later.")
            
        final_redirect_uri = f"{redirect_uri}?code={custom_auth_code}"
        log.info(f"Successfully generated code. Returning redirect URI: {final_redirect_uri}")
        return JSONResponse(content={"redirect_uri": final_redirect_uri})
        
    except HTTPException: raise
    except Exception as e:
        log.exception("An unexpected error occurred during authorization"); raise HTTPException(status_code=500, detail="An internal server error occurred")


@app.post("/token")
def handle_custom_token_exchange(code: str = Header(...)):
    if not ISSUE_JWT_URL:
        log.error("FATAL: ISSUE_JWT_URL environment variable is not set.")
        raise HTTPException(status_code=500, detail="Service is not configured correctly.")
        
    try:
        log.info(f"Received custom token exchange request.")
        auth_data = validate_and_get_data_from_code(code)
        if not auth_data:
            raise HTTPException(status_code=400, detail="Invalid, expired, or previously used code.")
        
        # MODIFIED: Retrieve all necessary data from Redis
        client_id = auth_data.get("client_id")
        client_secret = auth_data.get("client_secret")
        authenticated_msisdn = auth_data.get("msisdn")
        consumer_username = auth_data.get("consumer_username")

        if not all([client_id, client_secret, authenticated_msisdn, consumer_username]):
            raise HTTPException(status_code=500, detail="Stored authorization context is incomplete.")

        log.info(f"Auth code validated successfully for consumer: {consumer_username}")

        kong_token_url = KONG_INTERNAL_BASE.rstrip("/") + "/oauth2/token"
        kong_payload = { "grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret }
        log.info(f"Calling Kong's internal token endpoint to get an intermediate token.")
        kong_resp = requests.post(kong_token_url, data=kong_payload, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)
        
        if kong_resp.status_code != 200:
            log.error(f"Failed to get intermediate token from Kong. Status: {kong_resp.status_code}, Body: {kong_resp.text}")
            try: response_content = kong_resp.json()
            except requests.exceptions.JSONDecodeError: response_content = kong_resp.text
            raise HTTPException(status_code=502, detail={"error": "Failed to get intermediate token", "upstream_response": response_content})

        access_token = kong_resp.json().get("access_token")
        if not access_token:
            raise HTTPException(status_code=502, detail="Intermediate token request succeeded but no access_token was found.")
        
        log.info("Successfully retrieved intermediate access token from Kong.")

        # --- MODIFIED: Call jwt-issuer with the correct headers ---
        headers = {
            'Authorization': f'Bearer {access_token}',
            'X-Consumer-Username': consumer_username,
            'X-Login-Hint': authenticated_msisdn
        }
        
        log.info(f"Calling final JWT issuer service at {ISSUE_JWT_URL} for consumer: {consumer_username}")
        # Note: 'params' argument is removed
        final_jwt_resp = requests.get(ISSUE_JWT_URL, headers=headers, timeout=REQ_TIMEOUT, verify=VERIFY_TLS)

        log.info(f"Received response from final JWT issuer. Status: {final_jwt_resp.status_code}")
        try: final_response_content = final_jwt_resp.json()
        except requests.exceptions.JSONDecodeError: final_response_content = final_jwt_resp.text
        
        return JSONResponse(status_code=final_jwt_resp.status_code, content=final_response_content)

    except HTTPException: raise
    except requests.RequestException as e:
        log.exception("HTTP request error during token exchange orchestration")
        raise HTTPException(status_code=503, detail=f"A downstream service is unavailable: {e}")
    except Exception as e:
        log.exception("An unexpected error occurred during token exchange")
        raise HTTPException(status_code=500, detail="An internal server error occurred")