# FILE: jwt-issuer.py

import os
import time
from typing import Optional, Tuple

import jwt
import requests
from fastapi import FastAPI, Header, HTTPException, Response

app = FastAPI()

# --- Configuration ---
KONG_ADMIN_URL = os.environ.get("KONG_ADMIN_URL")  
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_LIFETIME = int(os.getenv("TOKEN_LIFETIME", "3600"))
# set to SSL verifycation, for cluster internal communication
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")

# In Memory Cache to avoid repeated Admin API calls
_cred_cache = {} 


def get_jwt_credential(username: str) -> Optional[Tuple[str, str]]:
    if username in _cred_cache:
        return _cred_cache[username]

    if not KONG_ADMIN_URL:
        print("ERROR: KONG_ADMIN_URL is not set")
        return None

    try:
        url = f"{KONG_ADMIN_URL}/consumers/{username}/jwt"
        resp = requests.get(url, timeout=5, verify=VERIFY_TLS)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        if data:
            cred = data[0]
            key = cred.get("key")
            secret = cred.get("secret")
            if key and secret:
                _cred_cache[username] = (key, secret)
                return key, secret
    except requests.RequestException as e:
        print(f"ERROR: fetching JWT credential for '{username}': {e}")
        return None

    print(f"WARN: No JWT credential for consumer '{username}'")
    return None


@app.get("/")
def issue_jwt(
    x_consumer_username: Optional[str] = Header(None),  
    x_login_hint: Optional[str] = Header(None),         
):
    if not x_consumer_username or not x_login_hint:
        raise HTTPException(
            status_code=400,
            detail="Missing required headers from Kong Gateway (X-Consumer-Username, X-Login-Hint)",
        )

    cred = get_jwt_credential(x_consumer_username)
    if not cred:
        raise HTTPException(
            status_code=500,
            detail=f"Could not find a valid JWT credential for consumer '{x_consumer_username}'",
        )

    key, secret = cred  

    now = int(time.time())
    payload = {
        "iss": key,                 
        "login_hint": x_login_hint,  
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
    }

    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)
    return {"jwt": token}


@app.get("/healthz")
def healthz():
    return Response(content="OK", status_code=200)
