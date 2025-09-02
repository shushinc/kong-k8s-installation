import json
import os
import logging
import base64
import time
import hmac
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Tuple

import boto3
from botocore.exceptions import ClientError
import redis
import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import JSONResponse, RedirectResponse, PlainTextResponse

# -----------------------
# Logging
# -----------------------
logger = logging.getLogger("auth-service")
handler = logging.StreamHandler()
formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

# -----------------------
# Environment
# -----------------------
# Required
REDIS_HOST = os.getenv("REDIS_HOST")
USER_POOL_ID = os.getenv("USER_POOL_ID")
AWS_REGION = os.getenv("AWS_REGION") or boto3.Session().region_name
COGNITO_DOMAIN = os.getenv("COGNITO_DOMAIN")

# Optional / defaults
REDIS_PORT = int(os.getenv("REDIS_PORT", "6380"))
REDIS_TTL = int(os.getenv("REDIS_TTL", "300"))
USERNAME = os.getenv("USERNAME", "authserveruser")
PASSWORD = os.getenv("PASSWORD", "Authserveruser@123.")
AUTH_SERVICE_BASE = os.getenv("AUTH_SERVICE_BASE", "http://34.54.169.57")  # change via env for other envs
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "3"))

# Validate required environment variables
required_env_vars = {
    "REDIS_HOST": REDIS_HOST,
    "USER_POOL_ID": USER_POOL_ID,
    "AWS_REGION": AWS_REGION,
    "COGNITO_DOMAIN": COGNITO_DOMAIN,
}
missing = [k for k, v in required_env_vars.items() if not v]
if missing:
    logger.error(f"Missing required environment variables: {', '.join(missing)}")

# -----------------------
# Redis (singleton)
# -----------------------
_redis_client = None


def get_redis_client() -> redis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.StrictRedis(
            host=REDIS_HOST, port=REDIS_PORT, decode_responses=True
        )
    return _redis_client


# -----------------------
# Small helpers
# -----------------------
def generate_auth_code(login_hint: str) -> str:
    """Generate a base64-encoded auth code from login_hint + timestamp."""
    ts = str(int(time.time()))
    raw = f"{login_hint}:{ts}"
    return base64.urlsafe_b64encode(raw.encode()).decode()


def decode_auth_code(auth_code: str) -> Tuple[Optional[str], Optional[str]]:
    """Decode a base64-encoded auth code to get login_hint and timestamp."""
    try:
        if not auth_code:
            return None, None
        raw = base64.urlsafe_b64decode(auth_code).decode()
        if ":" not in raw:
            return None, None
        login_hint, ts = raw.split(":")
        return login_hint, ts
    except Exception as e:
        logger.error(f"decode_auth_code: failed: {e}")
        return None, None


def is_auth_code_valid(auth_code: str) -> Tuple[Optional[str], Optional[str]]:
    """Decode and ensure non-expired by REDIS_TTL."""
    if not auth_code:
        return None, "Invalid token"
    login_hint, ts = decode_auth_code(auth_code)
    if not login_hint or not ts:
        return None, "Invalid token"
    try:
        auth_time = datetime.utcfromtimestamp(int(ts))
        expiry_time = auth_time + timedelta(seconds=REDIS_TTL)
        if datetime.utcnow() > expiry_time:
            return None, "Token is expired"
        return login_hint, None
    except ValueError:
        return None, "Invalid token"


def calculate_secret_hash(client_id: str, client_secret: str, username: str) -> str:
    """Generate the Cognito secret hash using HMAC-SHA256."""
    msg = username + client_id
    digest = hmac.new(client_secret.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def get_cognito_client():
    return boto3.client("cognito-idp", region_name=AWS_REGION)


def get_client_secret(client_id: str) -> Optional[str]:
    """Retrieve client secret from Cognito based on client_id."""
    try:
        client = get_cognito_client()
        resp = client.describe_user_pool_client(UserPoolId=USER_POOL_ID, ClientId=client_id)
        details = resp.get("UserPoolClient", {})
        secret = details.get("ClientSecret")
        if not secret:
            logger.error(f"Client secret not found for client_id={client_id}")
            return None
        return secret
    except ClientError as e:
        logger.error(f"Cognito error while fetching client_secret: {e}")
        return None


def validate_client(client_id: str, redirect_uri: str, scope: str) -> bool:
    """Validate client against Cognito config."""
    try:
        client = get_cognito_client()
        resp = client.describe_user_pool_client(UserPoolId=USER_POOL_ID, ClientId=client_id)
        details = resp.get("UserPoolClient", {})
        if not details:
            logger.error(f"Client {client_id} not found.")
            return False

        if redirect_uri not in (details.get("CallbackURLs") or []):
            logger.error(f"Invalid redirect URI: {redirect_uri} not in {details.get('CallbackURLs')}")
            return False

        if scope not in (details.get("AllowedOAuthScopes") or []):
            logger.error(f"Invalid scope: {scope} not in {details.get('AllowedOAuthScopes')}")
            return False

        return True
    except ClientError as e:
        logger.error(f"Cognito ClientError: {e}")
        return False
    except Exception as e:
        logger.error(f"Validation error: {e}")
        return False


def set_user_attribute(username: str, login_hint: str) -> bool:
    """Store login_hint in Cognito user attributes before auth."""
    try:
        client = get_cognito_client()
        client.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[{"Name": "custom:login_hint", "Value": login_hint}],
        )
        logger.info(f"User attribute custom:login_hint updated for {username}: {login_hint}")
        return True
    except ClientError as e:
        logger.error(f"Error updating user attributes: {e}")
        return False


def get_jwt_token(client_id: str, client_secret: str, scope: str, login_hint: str,
                  username: str, password: str) -> Optional[dict]:
    """Obtain a JWT token via ADMIN_NO_SRP_AUTH, pass metadata."""
    client = get_cognito_client()
    secret_hash = calculate_secret_hash(client_id, client_secret, username)
    set_user_attribute(username, login_hint)
    try:
        auth_resp = client.admin_initiate_auth(
            UserPoolId=USER_POOL_ID,
            ClientId=client_id,
            AuthFlow="ADMIN_NO_SRP_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
                "SECRET_HASH": secret_hash,
                "login_hint": "login_hint",
            },
            ClientMetadata={
                "login_hint": login_hint,
                "custom_context": "value_here",
            },
        )
        return auth_resp.get("AuthenticationResult")
    except ClientError as e:
        logger.error(f"Cognito auth error: {e.response.get('Error', {}).get('Message')}")
        return None


def extract_token_info(jwt_response: dict) -> dict:
    """Return only required fields."""
    return {
        "AccessToken": jwt_response.get("AccessToken"),
        "ExpiresIn": jwt_response.get("ExpiresIn"),
        "TokenType": jwt_response.get("TokenType"),
    }


# -----------------------
# HTTP session with retry
# -----------------------
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount("http://", HTTPAdapter(max_retries=retries))
session.mount("https://", HTTPAdapter(max_retries=retries))


def fetch_auth_service(auth_url: str) -> Optional[requests.Response]:
    """GET with retry + small timeout."""
    try:
        logger.info(f"Fetching auth service: {auth_url}")
        return session.get(auth_url, timeout=HTTP_TIMEOUT_SECONDS)
    except requests.Timeout:
        logger.error("Auth service timeout!")
        return None
    except requests.RequestException as e:
        logger.error(f"Auth service request error: {e}")
        return None


# -----------------------
# FastAPI
# -----------------------
app = FastAPI(title="Auth Service", version="1.0.0")


@app.get("/healthz")
def healthz():
    return PlainTextResponse("ok", status_code=200)


def pick(request: Request, key: str) -> Optional[str]:
    """
    Get a value from headers (case-insensitive) or query params.
    Header wins if present.
    """
    # Headers
    v = request.headers.get(key) or request.headers.get(key.replace("_", "-"))
    if v:
        return v
    # Query params
    return request.query_params.get(key)


@app.get("/authorizer")
async def authorizer(request: Request):
    # """
    # Validate client, call external auth service, issue 302 with code on success.
    # Required: client_id, redirect_uri, scope
    # Optional (but used): login_hint
    # """
    # client_id = pick(request, "client_id")
    # redirect_uri = pick(request, "redirect_uri")
    # scope = pick(request, "scope")
    # login_hint = pick(request, "login_hint")

    # masked_client = (client_id or "")[:4] + "****" if client_id else "None"
    # logger.info(f"/authorizer: client_id={masked_client}, login_hint={'set' if login_hint else 'missing'}")

    # # Missing required params
    # missing_params = []
    # if not client_id:
    #     missing_params.append("client_id")
    # if not redirect_uri:
    #     missing_params.append("redirect_uri")
    # if not scope:
    #     missing_params.append("scope")
    # if missing_params:
    #     return JSONResponse(
    #         status_code=400,
    #         content={"error": f"Missing parameters: {', '.join(missing_params)}"},
    #     )

    # # (Optional) Require login_hint if your flow needs it:
    # if not login_hint:
    #     return JSONResponse(
    #         status_code=400, content={"error": "Missing required parameters: login_hint"}
    #     )

    # # Validate client details in Cognito
    # if not validate_client(client_id, redirect_uri, scope):
    #     return JSONResponse(status_code=400, content={"error": "Invalid client credentials"})

    # # Encode + to %2B only if present (keep your original behavior)
    # if login_hint.startswith("+"):
    #     login_hint = login_hint.replace("+", "%2B")

    # auth_url = f"{AUTH_SERVICE_BASE}/v0/authenticate_user?msisdn={login_hint}"
    # resp = fetch_auth_service(auth_url)
    # if resp is None:
    #     return JSONResponse(status_code=502, content={"error": "Auth service unavailable"})

    # logger.info(f"Auth service response: {resp.status_code}, body: {resp.text[:200]}")

    # if resp.status_code == 200:
    #     auth_code = generate_auth_code(login_hint)
    #     # If you want to store the code in Redis, uncomment below:
    #     # ok = store_auth_code(login_hint, auth_code)
    #     # if not ok:
    #     #     return JSONResponse(status_code=500, content={"error": "Failed to store auth code"})
    #     return RedirectResponse(url=f"{redirect_uri}?code={auth_code}", status_code=302)

    # if resp.status_code == 404:
    #     return JSONResponse(status_code=404, content={"error": "Phone number not found"})

    # return JSONResponse(status_code=400, content={"error": "Phone number validation failed"})

    return {"status": "ok", "service": "camera-auth"}

@app.get("/token")
async def token(request: Request):
    # """
    # Exchange auth_code for Cognito token.
    # Required: client_id, scope, code
    # Optional: redirect_uri, login_hint (not required for exchange since code encodes it)
    # """
    # client_id = pick(request, "client_id")
    # scope = pick(request, "scope")
    # auth_code = pick(request, "code")

    # masked_client = (client_id or "")[:4] + "****" if client_id else "None"
    # logger.info(f"/token: client_id={masked_client}, code={'set' if auth_code else 'missing'}")

    # missing = []
    # if not client_id:
    #     missing.append("client_id")
    # if not scope:
    #     missing.append("scope")
    # if not auth_code:
    #     missing.append("code")
    # if missing:
    #     return JSONResponse(status_code=400, content={"error": f"Missing parameters: {', '.join(missing)}"})

    # # Get client secret
    # client_secret = get_client_secret(client_id)
    # if not client_secret:
    #     return JSONResponse(status_code=400, content={"error": "Invalid client_id or missing client_secret"})

    # # Validate & extract login_hint from code
    # login_hint, err = is_auth_code_valid(auth_code)
    # if err:
    #     return JSONResponse(status_code=400, content={"error": err})

    # logger.info(f"/token: auth code valid for login_hint={login_hint}")

    # # Retrieve JWT from Cognito
    # jwt = get_jwt_token(client_id, client_secret, scope, login_hint, USERNAME, PASSWORD)
    # if not jwt:
    #     return JSONResponse(status_code=500, content={"error": "Failed to retrieve JWT token"})

    # return JSONResponse(status_code=200, content=extract_token_info(jwt))
    return {"status": "ok", "service": "camera-auth"}

# Optional POST support mirroring GET (form or JSON)
@app.post("/authorizer")
async def authorizer_post(request: Request):
    # return await authorizer(request)
    return {"status": "ok", "service": "camera-auth"}


@app.post("/token")
async def token_post(request: Request):
    # return await token(request)
    return {"status": "ok", "service": "camera-auth"}
