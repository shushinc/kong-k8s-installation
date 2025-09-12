import os
import sys
import base64
import time
import httpx
import json
from fastapi import FastAPI, Request, Response, HTTPException, Header
from fastapi.responses import JSONResponse, PlainTextResponse
from typing import Tuple

# --- Environment Variables ---
BACKEND_API_URL = os.getenv("BACKEND_API_URL")
KONG_ADMIN_URL = os.getenv("KONG_ADMIN_URL")
KONG_INTERNAL_OAUTH_URL = os.getenv("KONG_INTERNAL_OAUTH_URL")
ISSUE_JWT_URL = os.getenv("ISSUE_JWT_URL")

missing_vars = []
if not BACKEND_API_URL: missing_vars.append("BACKEND_API_URL")
if not KONG_ADMIN_URL: missing_vars.append("KONG_ADMIN_URL")
if not KONG_INTERNAL_OAUTH_URL: missing_vars.append("KONG_INTERNAL_OAUTH_URL")
if not ISSUE_JWT_URL: missing_vars.append("ISSUE_JWT_URL")

if missing_vars:
    sys.stderr.write(f"Error: Required environment variables are not set: {', '.join(missing_vars)}\n")
    sys.exit(1)

app = FastAPI(
    title="TS43 Auth & Token Service",
    description="A service to issue cookies, auth_codes, and final JWT tokens.",
    version="3.2.0" # Updated version
)

# --- Helper Functions ---

def generate_session_cookie(eapid: str) -> str:
    if not isinstance(eapid, str) or not eapid: return ""
    try:
        random_bytes = os.urandom(32)
        encoded_random_part = base64.urlsafe_b64encode(random_bytes).decode('utf-8').rstrip('=')
        combined_string = f"{encoded_random_part},eapid:{eapid}"
        return base64.b64encode(combined_string.encode('utf-8')).decode('utf-8')
    except Exception as e:
        print(f"Error generating session cookie: {e}"); return ""

def generate_intermediate_code(seed_value: str) -> str:
    try:
        timestamp = str(int(time.time())).encode('utf-8')
        random_bytes = os.urandom(16)
        seed_bytes = seed_value.encode('utf-8')
        combined_raw = timestamp + b":" + random_bytes + b":" + seed_bytes
        return base64.urlsafe_b64encode(combined_raw).decode('utf-8').rstrip('=')
    except Exception as e:
        print(f"Error generating intermediate code: {e}"); return ""

def extract_eapid_from_header(header_value: str, header_name: str) -> str | None:
    if not header_value:
        print(f"Error: '{header_name}' header is missing.")
        return None
    try:
        padding = '=' * (4 - len(header_value) % 4)
        if header_value.startswith('(') and header_value.endswith(')'):
             header_value = header_value[1:-1]
        decoded_string = base64.b64decode(header_value + padding).decode('utf-8')
        parts = decoded_string.split(',eapid:')
        if len(parts) >= 2:
            eapid = parts[1].split(')')[0] 
            print(f"Successfully extracted eapid '{eapid}' from {header_name} header.")
            return eapid
        else:
            print(f"Error: Decoded '{header_name}' is not in the expected format: {decoded_string}")
            return None
    except Exception as e:
        print(f"Error decoding '{header_name}' header: {e}")
        return None

async def get_consumer_details_from_kong(client_id: str) -> Tuple[str, str] | None:
    oauth2_url = f"{KONG_ADMIN_URL.rstrip('/')}/oauth2"
    params = {"client_id": client_id}
    try:
        async with httpx.AsyncClient(verify=False) as client:
            print(f"Querying Kong Admin API for secret: {oauth2_url}")
            oauth_resp = await client.get(oauth2_url, params=params, timeout=5.0)
            oauth_resp.raise_for_status()
            oauth_data = oauth_resp.json()

            if not (oauth_data.get("data") and oauth_data["data"]):
                print(f"Warning: No OAuth2 credential found for client_id: {client_id}")
                return None
            
            credential = oauth_data["data"][0]
            client_secret = credential.get("client_secret")
            consumer_id = credential.get("consumer", {}).get("id")

            if not client_secret or not consumer_id:
                print(f"Error: Incomplete credential data for client_id: {client_id}")
                return None
            
            consumer_url = f"{KONG_ADMIN_URL.rstrip('/')}/consumers/{consumer_id}"
            print(f"Querying Kong Admin API for username: {consumer_url}")
            consumer_resp = await client.get(consumer_url, timeout=5.0)
            consumer_resp.raise_for_status()
            consumer_data = consumer_resp.json()
            consumer_username = consumer_data.get("username")

            if not consumer_username:
                print(f"Error: Could not find username for consumer_id: {consumer_id}")
                return None
            
            print(f"Successfully retrieved secret and username '{consumer_username}' for client_id: {client_id}")
            return client_secret, consumer_username

    except (httpx.RequestError, httpx.HTTPStatusError) as e:
        print(f"Error interacting with Kong Admin API: {e}"); return None

# --- API Endpoints ---

@app.get("/healthz")
def healthz(): return {"status": "ok"}

@app.get("/v2/authenticate_ts43_client")
async def proxy_and_set_cookie(request: Request):
    eapid_header = request.headers.get("eapid")
    if not eapid_header:
        return PlainTextResponse("Bad Request: Missing 'eapid' header.", status_code=400)
    async with httpx.AsyncClient() as client:
        try:
            url = f"{BACKEND_API_URL}{request.url.path}?{request.query_params}"
            backend_response = await client.get(url, headers=dict(request.headers), timeout=10.0)
        except httpx.RequestError as e:
            return PlainTextResponse(f"Bad Gateway: {e}", status_code=502)
    client_response = Response(content=backend_response.content, status_code=backend_response.status_code, headers=dict(backend_response.headers))
    if backend_response.status_code == 200:
        cookie_value = generate_session_cookie(eapid_header)
        if cookie_value:
            client_response.set_cookie(key="session_id", value=cookie_value, path="/", samesite="lax")
    return client_response

@app.post("/v2/authenticate_ts43_client")
async def proxy_and_generate_authcode(request: Request):
    setcookie_header = request.headers.get("setcookie")
    extracted_eapid = extract_eapid_from_header(setcookie_header, "setcookie")
    if not extracted_eapid:
        return PlainTextResponse("Bad Request: Invalid or missing 'setcookie' header.", status_code=400)
    async with httpx.AsyncClient() as client:
        try:
            url = f"{BACKEND_API_URL}{request.url.path}"
            backend_response = await client.post(url, headers=dict(request.headers), content=await request.body(), timeout=10.0)
        except httpx.RequestError as e:
            return PlainTextResponse(f"Bad Gateway: {e}", status_code=502)
    if backend_response.status_code != 200:
        return Response(content=backend_response.content, status_code=backend_response.status_code, headers=dict(backend_response.headers))
    try:
        backend_body_json = backend_response.json()
        cookie_from_backend = backend_body_json.get("setCookie")
        if not cookie_from_backend: return JSONResponse(content=backend_body_json, status_code=200)
        intermediate_code = generate_intermediate_code(cookie_from_backend)
        final_string = f"({intermediate_code},eapid:{extracted_eapid})"
        backend_body_json["auth_code"] = base64.b64encode(final_string.encode('utf-8')).decode('utf-8')
        return JSONResponse(content=backend_body_json, status_code=200)
    except json.JSONDecodeError:
        return PlainTextResponse("Bad Gateway: Upstream returned non-JSON.", status_code=502)

@app.get("/v2/ts43_operator_token")
async def get_final_jwt_token(
    client_id: str = Header(..., alias="client_id"),
    auth_code: str = Header(..., alias="auth_code")
):
    print(f"Received token request for client_id: {client_id}")
    
    consumer_details = await get_consumer_details_from_kong(client_id)
    if not consumer_details:
        raise HTTPException(status_code=401, detail="Invalid client_id or credentials not found.")
    client_secret, consumer_username = consumer_details

    kong_token_url = f"{KONG_INTERNAL_OAUTH_URL.rstrip('/')}/oauth2/token"
    kong_payload = {"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret}
    
    try:
        async with httpx.AsyncClient(verify=False) as client:
            print(f"Requesting Kong access token from {kong_token_url}")
            kong_resp = await client.post(kong_token_url, data=kong_payload, timeout=10.0)
        
        if kong_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Failed to retrieve intermediate token from auth server.")
        
        kong_access_token = kong_resp.json().get("access_token")
        if not kong_access_token:
            raise HTTPException(status_code=502, detail="Intermediate token response did not contain an access_token.")
        
        print("Successfully retrieved Kong access token.")

        eapid_for_jwt = extract_eapid_from_header(auth_code, "auth_code")
        if not eapid_for_jwt:
            raise HTTPException(status_code=400, detail="Invalid or malformed auth_code header.")

        jwt_issuer_url = f"{ISSUE_JWT_URL.rstrip('/')}"
        jwt_headers = {
            'Authorization': f'Bearer {kong_access_token}',
            'X-Consumer-Username': consumer_username,
            'X-Login-Hint': eapid_for_jwt
        }
        jwt_params = {'login_hint': eapid_for_jwt}

        async with httpx.AsyncClient(verify=False) as client:
            print(f"Requesting final JWT from {jwt_issuer_url} for consumer: {consumer_username}")
            final_jwt_resp = await client.get(jwt_issuer_url, headers=jwt_headers, params=jwt_params, timeout=10.0)

        if final_jwt_resp.status_code != 200:
            return Response(
                content=final_jwt_resp.content,
                status_code=final_jwt_resp.status_code,
                headers={"Content-Type": "application/json"}
            )
        
        try:
            final_jwt_json = final_jwt_resp.json()
            final_jwt_json["token_type"] = "bearer"
            final_jwt_json["expires_in"] = 7200 # As requested, fixed value
            
            print("Successfully added token_type and expires_in to the final JWT response.")
            return JSONResponse(content=final_jwt_json, status_code=200)
        
        except json.JSONDecodeError:
            # This is an edge case in case the JWT issuer returns 200 OK but not valid JSON
            return PlainTextResponse("Bad Gateway: Final token issuer returned invalid JSON.", status_code=502)

    except httpx.RequestError as e:
        print(f"HTTP Error during token exchange: {e}")
        raise HTTPException(status_code=503, detail="A downstream service is unavailable.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        raise HTTPException(status_code=500, detail="An internal server error occurred.")

