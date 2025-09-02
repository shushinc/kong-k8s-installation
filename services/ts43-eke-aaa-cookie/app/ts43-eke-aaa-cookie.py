import os
import redis
import base64
import datetime
import secrets
from fastapi import FastAPI, Request, HTTPException

app = FastAPI()

# Environment variables
REDIS_HOST = os.environ["REDIS_HOST"]
REDIS_PORT = int(os.environ["REDIS_PORT"])

# Initialize Redis client WITH TLS
redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    ssl=True,
    decode_responses=True
)

@app.get("/health")
async def health():
    try:
        redis_client.ping()
        return {"status": "ok", "redis": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Redis error: {str(e)}")

@app.on_event("startup")
def startup_event():
    try:
        redis_client.ping()
        print("✅ Connected to Redis")
    except Exception as e:
        print("❌ Redis connection failed:", str(e))
        raise

@app.get("/authorize")
async def authorize(request: Request):
    # Token puede venir de header auth_code o query param
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

    # Generar flow_id seguro
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
