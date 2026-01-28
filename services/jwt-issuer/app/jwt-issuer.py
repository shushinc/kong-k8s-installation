import os
import time
from typing import Optional, Tuple
from fastapi import Header, HTTPException
import time
import jwt
import requests
import logging
from fastapi import FastAPI, Header, HTTPException, Response
from logging.handlers import RotatingFileHandler

# configuration
KONG_ADMIN_URL = os.environ.get("KONG_ADMIN_URL")  
JWT_ALGORITHM = os.getenv("JWT_ALGORITHM", "HS256")
TOKEN_LIFETIME = int(os.getenv("TOKEN_LIFETIME", "3600"))
# set to SSL verifycation, for cluster internal communication
VERIFY_TLS = os.getenv("VERIFY_TLS", "false").lower() in ("1", "true", "yes")
x_scope: Optional[str] = Header(None),

#inmemory cache to avoid repeated Admin API calls
_cred_cache = {} 



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

app = FastAPI()


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
    x_scope: Optional[str] = Header(None, alias="X-Scope"),
    scope: Optional[str] = Header(None, alias="scope"),
):
    log.info("JWT issue request received")

    if not x_consumer_username or not x_login_hint:
        log.warning(
            "Missing required headers | consumer=%s login_hint=%s",
            x_consumer_username,
            x_login_hint,
        )
        raise HTTPException(
            status_code=400,
            detail="Missing required headers (X-Consumer-Username, X-Login-Hint)",
        )

    log.debug(
        "Headers received | consumer=%s login_hint=%s raw_scope=%s",
        x_consumer_username,
        x_login_hint,
        x_scope or scope,
    )

    cred = get_jwt_credential(x_consumer_username)
    if not cred:
        log.error(
            "JWT credential not found for consumer=%s",
            x_consumer_username,
        )
        raise HTTPException(
            status_code=500,
            detail=f"JWT credential not found for consumer '{x_consumer_username}'",
        )

    key, secret = cred
    now = int(time.time())

    scope_val = (x_scope or scope or "").strip()

    payload = {
        "iss": key,
        "login_hint": x_login_hint,
        "iat": now,
        "exp": now + TOKEN_LIFETIME,
    }

    if scope_val:
        payload["scope"] = scope_val

    log.debug(
        "JWT payload prepared | iss=%s exp=%s scope=%s",
        payload["iss"],
        payload["exp"],
        payload.get("scope"),
    )

    token = jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)

    log.info(
        "JWT issued successfully | consumer=%s scopes=%s",
        x_consumer_username,
        scope_val or "<none>",
    )

    return {"jwt": token}


@app.get("/healthz")
def healthz():
    return Response(content="OK", status_code=200)

