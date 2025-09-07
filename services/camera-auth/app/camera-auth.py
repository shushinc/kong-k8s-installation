#!/usr/bin/env python3
import os
import logging
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any

import requests
from fastapi import FastAPI, Response, Form, HTTPException
from fastapi.responses import JSONResponse

# (Keep all configuration and logging setup the same)
# ...
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("camera-auth")
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")
REQ_TIMEOUT = float(os.getenv("HTTP_TIMEOUT_SECONDS", "10"))
#KONG_INTERNAL_BASE = os.getenv("KONG_INTERNAL_OAUTH_URL", "https://kong-kong-proxy.kong.svc.cluster.local:443").strip()
KONG_INTERNAL_BASE = os.environ["KONG_INTERNAL_OAUTH_URL"].strip()
#EXTERNAL_AUTH_URL = os.getenv("EXTERNAL_AUTH_URL", "http://34.54.169.57/v0/authenticate_user").strip()
EXTERNAL_AUTH_URL = os.environ["EXTERNAL_AUTH_URL"].strip()


app = FastAPI(
    title="Camera Authorizer Service",
    description="Orchestrates external authentication and the Kong OAuth2 flow.",
    version="1.0.0"
)

# (Keep /healthz and parse_code_from_uri functions the same)
# ...
@app.get("/healthz")
def healthz():
    return {"status": "ok"}

def parse_code_from_uri(uri: str) -> str | None:
    try:
        parsed_url = urlparse(uri)
        query_params = parse_qs(parsed_url.query)
        return query_params.get("code", [None])[0]
    except Exception as e:
        log.error(f"Failed to parse code from URI: {uri}. Error: {e}")
        return None

# --- Main Authorization Endpoint (with improved error handling) ---
@app.post("/")
def handle_authorization(
    response: Response,
    # (Keep all Form parameters the same)
    # ...
    response_type: str = Form(...),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    scope: str = Form(None),
    provision_key: str = Form(...),
    authenticated_userid: str = Form(...),
    login_hint: str = Form(...),
    identifier: str = Form(...),
    carrierName: str = Form(...),
    customerName: str = Form(...),
    ipAddress: str = Form(...),
    grant_type: str = Form(None)
):
    try:
        log.info(f"Received authorization request for login_hint (msisdn): {login_hint}")

        # --- Step 1: Call External Authentication API ---
        auth_params = {
            "identifier": identifier,
            "carrierName": carrierName,
            "customerName": customerName,
            "msisdn": login_hint,
            "ipAddress": ipAddress
        }

        log.info(f"Calling external auth API: {EXTERNAL_AUTH_URL} with params: {auth_params}")
        external_resp = requests.get(
            EXTERNAL_AUTH_URL,
            params=auth_params,
            timeout=REQ_TIMEOUT,
            verify=VERIFY_TLS
        )
        
        # --- NEW: Improved error forwarding ---
        if external_resp.status_code != 200:
            log.warning(f"External auth failed with status {external_resp.status_code}: {external_resp.text}")
            
            # Try to parse the JSON error from the external service
            try:
                error_detail = external_resp.json()
            except requests.exceptions.JSONDecodeError:
                error_detail = {"detail": external_resp.text or "Unknown error from external service"}

            # Forward the original status code and detail message
            raise HTTPException(
                status_code=external_resp.status_code, 
                detail=error_detail.get("detail", error_detail)
            )

        log.info("External authentication successful.")

        # --- Step 2: Call Kong's internal /oauth2/authorize endpoint ---
        kong_authorize_url = KONG_INTERNAL_BASE.rstrip("/") + "/oauth2/authorize"
        kong_payload = {
            "response_type": response_type,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scope,
            "provision_key": provision_key,
            "authenticated_userid": authenticated_userid,
            "login_hint": login_hint
        }
        kong_payload = {k: v for k, v in kong_payload.items() if v is not None}

        log.info(f"Calling Kong's internal authorize endpoint: {kong_authorize_url}")
        kong_resp = requests.post(
            kong_authorize_url,
            data=kong_payload,
            timeout=REQ_TIMEOUT,
            verify=VERIFY_TLS
        )

        if kong_resp.status_code >= 400:
            log.error(f"Kong returned an error. Status: {kong_resp.status_code}. Response: {kong_resp.text}")
            # Forward the exact error from Kong
            return JSONResponse(status_code=kong_resp.status_code, content={"error_detail": kong_resp.text})

        # --- Step 3: Success. Extract code and return redirect URI ---
        response_data = kong_resp.json()
        redirect_uri_from_kong = response_data.get("redirect_uri")
        if not redirect_uri_from_kong or not parse_code_from_uri(redirect_uri_from_kong):
            log.error(f"Kong response is missing or malformed: {response_data}")
            raise HTTPException(status_code=502, detail="Invalid response from Kong authorize endpoint")

        log.info("Successfully obtained auth code from Kong.")
        return JSONResponse(content={"redirect_uri": redirect_uri_from_kong})

    # --- NEW: Restructured Exception Handling ---
    except requests.RequestException as e:
        log.exception("HTTP request error during authorization flow")
        raise HTTPException(status_code=503, detail=f"Service unavailable: {e}")
    except HTTPException:
        # Re-raise HTTPException so FastAPI can handle it
        raise
    except Exception as e:
        # This now only catches truly unexpected errors
        log.exception("An unexpected error occurred")
        raise HTTPException(status_code=500, detail="An internal server error occurred")