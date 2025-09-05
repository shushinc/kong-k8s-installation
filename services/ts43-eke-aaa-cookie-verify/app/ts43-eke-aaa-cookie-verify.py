import os
import redis
import json
import base64
import requests
from requests.auth import HTTPBasicAuth
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError
from requests.exceptions import ConnectTimeout, ConnectionError as RequestsConnectionError
import uvicorn

app = FastAPI(title="TS43 cookie verify")

REDIS_HOST = os.environ.get("REDIS_HOST")
REDIS_PORT = int(os.environ.get("REDIS_PORT", 6379))
KONG_DOMAIN = os.environ.get("KONG_DOMAIN")
SHERLOCK_BACKEND_HOST = os.environ.get("SHERLOCK_BACKEND_HOST")


@app.get("/health")
def health_check():
    return {"status": "ok"}

def decode_flow_id(cookie_header: str):
    if "flow_id=" in cookie_header:
        for pair in cookie_header.split(";"):
            if pair.strip().startswith("flow_id="):
                flow_id_encoded = pair.strip().split("=")[1]
                break
    else:
        flow_id_encoded = cookie_header.strip()

    if not flow_id_encoded:
        return None

    try:
        decoded_value = base64.urlsafe_b64decode(flow_id_encoded + "===").decode("utf-8")
        flow_id = decoded_value.split(",")[0]
        return flow_id
    except Exception:
        return None


def get_credentials_from_redis(flow_id: str):
    try:
        redis_client = redis.Redis(
            host=REDIS_HOST,
            port=REDIS_PORT,
            ssl=True,
            socket_timeout=3,
            decode_responses=True
        )
        redis_client.ping()

        auth_code_key = f"flow_id:{flow_id}"
        auth_code = redis_client.get(auth_code_key)
        if not auth_code:
            return None, None

        encoded_credentials_key = f"auth_code:{auth_code}"
        encoded_credentials = redis_client.get(encoded_credentials_key)
        if not encoded_credentials:
            return None, None

        decoded = base64.b64decode(encoded_credentials).decode("utf-8")
        client_id, client_secret = decoded.split(":", 1)
        return client_id, client_secret

    except (RedisConnectionError, RedisTimeoutError):
        raise HTTPException(status_code=503, detail="Redis unavailable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")


def request_kong_token(client_id: str, client_secret: str):
    try:
        token_url = f"https://{KONG_DOMAIN}/oauth2/token"
        response = requests.post(
            token_url,
            data={"grant_type": "client_credentials", "scope": "sherlockapiresource/write"},
            auth=HTTPBasicAuth(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=5
        )
        if response.status_code != 200:
            raise HTTPException(status_code=401, detail="Kong token fetch failed")
        return response.json()
    except ConnectTimeout:
        raise HTTPException(status_code=504, detail="Kong request timed out")
    except RequestsConnectionError:
        raise HTTPException(status_code=503, detail="Kong connection failed")


@app.post("/authenticate_ts43_client")
async def authenticate(request: Request):
    headers = request.headers
    cookie_header = headers.get("setcookie") or headers.get("cookie")
    if not cookie_header:
        raise HTTPException(status_code=400, detail="Missing Set-Cookie header")

    flow_id = decode_flow_id(cookie_header)
    if not flow_id:
        raise HTTPException(status_code=401, detail="Invalid flow_id in cookie")

    client_id, client_secret = get_credentials_from_redis(flow_id)
    if not client_id or not client_secret:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token_data = request_kong_token(client_id, client_secret)

    try:
        body = await request.body()
        backend_url = f"{SHERLOCK_BACKEND_HOST}/v2/authenticate_ts43_client"
        backend_headers = {
            "Content-Type": headers.get("content-type", "application/json"),
            "eapid": headers.get("eapid"),
            "apihost": headers.get("apihost"),
            "setCookie": cookie_header
        }
        backend_response = requests.post(
            backend_url,
            data=body,
            headers=backend_headers,
            timeout=5
        )
        backend_response_json = backend_response.json()
    except ConnectTimeout:
        raise HTTPException(status_code=504, detail="Backend request timed out")
    except RequestsConnectionError:
        raise HTTPException(status_code=503, detail="Backend connection failed")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backend error: {str(e)}")

    # Merge responses
    if isinstance(backend_response_json, str):
        backend_response_json = json.loads(backend_response_json)

    merged_response = {
        **backend_response_json,
        "access_token": token_data.get("access_token"),
        "expires_in": token_data.get("expires_in"),
        "token_type": token_data.get("token_type")
    }
    return JSONResponse(content=merged_response)
