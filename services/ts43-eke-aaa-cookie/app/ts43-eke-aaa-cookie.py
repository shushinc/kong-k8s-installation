import os
import redis
import base64
import datetime
import secrets
import logging
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Environment variables (pero no conectamos aún)
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = os.getenv("REDIS_PORT")

def get_redis_client():
    if not REDIS_HOST or not REDIS_PORT:
        raise RuntimeError("REDIS_HOST or REDIS_PORT not set")
    return redis.Redis(
        host=REDIS_HOST,
        port=int(REDIS_PORT),
        ssl=True,
        decode_responses=True
    )

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/v2/authenticate_ts43_client")
async def authenticate_ts43_client(request: Request):
    try:
        redis_client = get_redis_client()
        redis_client.ping()
        logging.info("Successfully connected to Redis")
    except Exception as e:
        logging.error(f"Redis connection failed: {e}")
        raise HTTPException(status_code=500, detail="Redis unavailable")

    token = (
        request.headers.get("auth_code")
        or request.query_params.get("authorizationToken")
    )

    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized: No token provided")

    redis_key = f"auth_code:{token.strip()}"
    if not redis_client.exists(redis_key):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid token")

    eapid = request.headers.get("eapid", "")

    flow_id = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    redis_client.setex(f"flow_id:{flow_id}", datetime.timedelta(minutes=5), token)

    combined_value = f"{flow_id},eapid:{eapid}"
    encoded_value = base64.urlsafe_b64encode(combined_value.encode()).decode()

    return {
        "principalId": "ts43-client",
        "authorized": True,
        "flow_id": encoded_value,
        "eapid": eapid
    }
