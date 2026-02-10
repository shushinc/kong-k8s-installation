import os
import json
import logging
import threading
from collections import defaultdict
from datetime import datetime, timezone
import csv
import io
import re
import time  
import requests
from requests.auth import HTTPBasicAuth
import redis
from datetime import datetime

from flask import Flask, request, jsonify

# Optional BigQuery imports — only used if env vars are provided
try:
    from google.cloud import bigquery
    from google.oauth2 import service_account
except Exception:  # pragma: no cover
    bigquery = None
    service_account = None

# --- Config -----------
# bq config via environment variables
BQ_PROJECT_ID = os.getenv("BQ_PROJECT_ID")              
BQ_DATASET = os.getenv("BQ_DATASET")                   
BQ_TABLE = os.getenv("BQ_TABLE")                       
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")  
CARRIER_NAME_OVERRIDE = os.getenv("CARRIER_NAME") or os.getenv("CARRIER_NAME_RAW")
PRICING_FILE_PATH = os.getenv("PRICING_FILE_PATH", "/config/pricing.csv")
# kong cluster admin for pricing-type lookup
KONG_ADMIN_URL = os.getenv("KONG_ADMIN_URL")  # e.g. https://kong-kong-admin.kong.svc.cluster.local:8444
KONG_ADMIN_TOKEN = os.getenv("KONG_ADMIN_TOKEN")  # rightnow, kong admin endpoint are open
DEFAULT_PRICING_TYPE = (os.getenv("DEFAULT_PRICING_TYPE") or "international").strip().lower()
CLIENT_PRICING_CACHE_TTL = int(os.getenv("CLIENT_PRICING_CACHE_TTL", "300"))  # seconds

# === Drupal analytics push (optional) ===
DRUPAL_ANALYTICS_URL = (os.getenv("DRUPAL_ANALYTICS_URL") or "").strip()
DRUPAL_BASIC_USER = (os.getenv("DRUPAL_BASIC_USER") or "").strip()
DRUPAL_BASIC_PASS = (os.getenv("DRUPAL_BASIC_PASS") or "").strip()
DRUPAL_TIMEOUT_SECONDS = float(os.getenv("DRUPAL_TIMEOUT_SECONDS", "10"))

# redis config
REDIS_HOST = os.getenv("REDIS_HOST", "ts43-redis.kong.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
# prefix for client profile keys in Redis
REDIS_CLIENT_PROFILE_PREFIX = os.getenv("REDIS_CLIENT_PROFILE_PREFIX", "client_profile:")


_pricing_lock = threading.Lock()
_pricing_table = {}
_pricing_loaded = False

# client -> (pricing_type, cached_at_epoch)
_client_pricing_cache = {}
_client_pricing_lock = threading.Lock()

# client -> (client_type, cached_at_epoch)
_client_type_cache = {}
_client_type_lock = threading.Lock()

# flask app
app = Flask(__name__)
# ---- logging / debug mode ----
_LOG_LEVEL_RAW = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
_DEBUG_RAW = (os.getenv("DEBUG") or "").strip().lower()
if _DEBUG_RAW in ("1", "true", "yes", "y", "on"):
    _LOG_LEVEL_RAW = "DEBUG"

LOG_LEVEL = getattr(logging, _LOG_LEVEL_RAW, logging.INFO)
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")

# ---- logging setup (REQUIRED) ----
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger(__name__)

def _debug_json(label: str, obj, max_chars: int = 20000):
    # Only log when logger is in DEBUG level
    if not logging.getLogger().isEnabledFor(logging.DEBUG):
        return

    try:
        import json
        s = json.dumps(obj, indent=2, default=str)
        if len(s) > max_chars:
            s = s[:max_chars] + "\n...<truncated>..."
        logging.debug("%s:\n%s", label, s)
    except Exception as e:
        logging.exception("Debug JSON failed for %s: %s", label, e)


#- in-memry store---
_aggr = defaultdict(lambda: {
    "count": 0,
    "200": {"count": 0, "latency_sum": 0.0},
    "404": {"count": 0, "latency_sum": 0.0},
    "other": {"count": 0, "latency_sum": 0.0}
})
_lock = threading.Lock()

redis_client = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    password=REDIS_PASSWORD,  
    decode_responses=True,
)

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
                socket_timeout=2,
                socket_connect_timeout=2,
            )
            _redis_client.ping()
            log.info("Successfully connected to Redis.")
        except Exception as e:
            log.error(f"Could not connect to Redis: {e}", exc_info=True)
            _redis_client = None
            raise
    return _redis_client

def _load_pricing_locked() -> None:
    """
    Load pricing from CSV into an in-memory dict.
    """
    global _pricing_table, _pricing_loaded

    path = PRICING_FILE_PATH
    table = {}

    if not path or not os.path.exists(path):
        logging.warning("Pricing file not found: %s", path)
        _pricing_table = {}
        _pricing_loaded = True
        return

    def _safe_float(value):
        if value is None:
            return None
        try:
            s = str(value).strip()
            if not s:
                return None
            return float(s)
        except Exception:
            return None

    try:
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                endpoint = (row.get("Endpoint") or "").strip()
                if not endpoint:
                    continue

                # Normalize endpoint key (strip trailing slash)
                endpoint_key = endpoint.rstrip("/")

                # Try to read separate domestic / international prices if present
                intl_raw = (
                    row.get("InternationalPrice")
                    or row.get("International Price")
                    or row.get("internationalprice")
                    or row.get("Price")
                    or ""
                )
                dom_raw = (
                    row.get("DomesticPrice")
                    or row.get("Domestic Price")
                    or ""
                )

                intl_price = _safe_float(intl_raw)
                dom_price = _safe_float(dom_raw)

                # Markup %
                markup_raw = (
                    row.get("Markup")
                    or row.get("Markup%")
                    or row.get("Markup Percent")
                    or row.get("Markup_Percent")
                    or row.get("MarkupPercentage")
                    or row.get("Markup Percentage")
                    or ""
                )
                markup_percent = _safe_float(markup_raw) or 0.0

                # Human-friendly attribute/name from pricing CSV
                api_attr = (
                    row.get("API") 
                    or row.get("APIAttributesName")
                    or row.get("API Attribute")
                    or row.get("APIAttributes")       
                    or row.get("Attribute")
                    or ""
                )
                api_attr = str(api_attr).strip() if api_attr is not None else ""

                if intl_price is None and dom_price is None:
                    continue

                table[endpoint_key] = {
                    "international": intl_price,
                    "domestic": dom_price,
                    "markup": markup_percent,
                    "api_attribute": api_attr,   # <-- NEW
                }

        _pricing_table = table
        _pricing_loaded = True
        logging.info(
            "Loaded %d pricing rows from %s",
            len(_pricing_table),
            path,
        )
    except Exception as e:
        logging.error("Failed to load pricing file '%s': %s", path, e)
        _pricing_table = {}
        _pricing_loaded = True

def _api_attribute_for_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None

    # Lazy-load pricing if needed (same approach as _price_for_endpoint)
    if not _pricing_loaded:
        with _pricing_lock:
            if not _pricing_loaded:
                _load_pricing_locked()

    key = str(endpoint).strip().split("?", 1)[0].rstrip("/") or "/"
    row = (_pricing_table or {}).get(key) or {}
    val = row.get("api_attribute") or ""
    val = str(val).strip() if val is not None else ""
    return val or None
        
def _price_for_endpoint(endpoint: str | None, pricing_type: str | None = None) -> float:
    """
    lookup the  per api call price for the given endpoint and prcing type
    """
    global _pricing_loaded

    if not endpoint:
        return 0.0

    # Normalize endpoint: strip query and trailing slash
    key = endpoint.split("?", 1)[0].rstrip("/") or "/"

    if pricing_type:
        ptype = str(pricing_type).strip().lower()
    else:
        ptype = DEFAULT_PRICING_TYPE

    # Lazy-load pricing if needed
    if not _pricing_loaded:
        with _pricing_lock:
            if not _pricing_loaded:
                _load_pricing_locked()

    val = _pricing_table.get(key, 0.0)

    # Backward-compatible: simple float or numeric string
    if isinstance(val, (int, float, str)):
        try:
            return float(val)
        except Exception:
            return 0.0

    # New structure: dict with "international" / "domestic"
    if isinstance(val, dict):
        # Try preferred pricing type first
        primary = val.get(ptype)
        if primary is not None:
            try:
                return float(primary)
            except Exception:
                pass

        # Fallback to any available one in deterministic order
        for candidate in ("international", "domestic"):
            candidate_val = val.get(candidate)
            if candidate_val is not None:
                try:
                    return float(candidate_val)
                except Exception:
                    continue

    return 0.0

def _markup_for_endpoint(endpoint: str | None) -> float:
    """
    markup % values set at moriarty after ratesheet approval, its client profile.
    for the given api attributes markup percentage : (0-100) 
    returns 0.0 if not configured 
    """
    global _pricing_loaded

    if not endpoint:
        return 0.0

    # Normalize endpoint
    key = endpoint.split("?", 1)[0].rstrip("/") or "/"

    if not _pricing_loaded:
        with _pricing_lock:
            if not _pricing_loaded:
                _load_pricing_locked()

    val = _pricing_table.get(key)
    if isinstance(val, dict):
        try:
            m = val.get("markup")
            return float(m) if m is not None else 0.0
        except Exception:
            return 0.0

    # If pricing table stored simple scalar values, no markup was defined
    return 0.0

# -------- Helpers Func------
def _parse_hour_bucket(ts_str: str | None, fallback_str: str | None) -> str:
    dt = None
    if ts_str:
        # Expecting 'YYYY-MM-DD HH:MM:SS' need to match wt
        try:
            dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            dt = None
    if dt is None and fallback_str:
        try:
            # Accepts '...Z' or offset; normalize to UTC
            # Example: '2025-11-03T10:56:16Z' ( normaliza the time in carrrier server)
            ts = fallback_str.rstrip("Z")
            if "+" in ts or "-" in ts[10:]:
                dt = datetime.fromisoformat(fallback_str.replace("Z", "+00:00")).astimezone(timezone.utc)
            else:
                dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        except Exception:
            dt = None
    if dt is None:
        dt = datetime.utcnow().replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    dt_floor = dt.replace(minute=0, second=0, microsecond=0)
    return dt_floor.isoformat().replace("+00:00", "Z")


def _status_bucket(code: int | str) -> str:
    try:
        c = int(code)
    except Exception:
        return "other"
    if c == 200:
        return "200"
    if c == 404:
        return "404"
    return "other"

def _get_client_pricing_type(client: str | None) -> str:
    """
    get the client pricing - domestic or international
    logic:
      1. in-memroy cache - _client_pricing_cache with TTL
      2. redis will have client profile (key: client_profile:<client>)
    """
    client = (client or "").strip()
    if not client or client.lower() == "unknown":
        return DEFAULT_PRICING_TYPE

    now = time.time()

    ## 1)in-memory cache lookup
    with _client_pricing_lock:
        cached = _client_pricing_cache.get(client)
        if cached:
            cached_type, ts = cached
            if now - ts < CLIENT_PRICING_CACHE_TTL:
                return cached_type

    # 2) redis lookup (client_profile:<client>)
    pricing_type = DEFAULT_PRICING_TYPE
    try:
        redis_key = f"{REDIS_CLIENT_PROFILE_PREFIX}{client}"
        raw = redis_client.get(redis_key)
        if raw:
            try:
                profile = json.loads(raw)
                pricing_type = (profile.get("pricing_type") or DEFAULT_PRICING_TYPE).strip().lower()
                if pricing_type not in ("domestic", "international"):
                    pricing_type = DEFAULT_PRICING_TYPE

                # cache it in memory for fast future lookups
                with _client_pricing_lock:
                    _client_pricing_cache[client] = (pricing_type, now)

                return pricing_type
            except Exception as e:
                logging.warning(
                    "Failed to parse Redis client_profile for %s: %s", client, e
                )
    except Exception as e:
        logging.warning("Redis lookup failed for client %s: %s", client, e)

    # 3) Remote lookup (Kong Admin) if configured
    if KONG_ADMIN_URL:
        try:
            url = f"{KONG_ADMIN_URL.rstrip('/')}/consumers/{client}"
            headers = {}
            resp = requests.get(url, timeout=2)
            if resp.ok:
                data = resp.json()
                tags = data.get("tags") or []
                tags_lower = [str(t).lower() for t in tags]
                if "pricing_domestic" in tags_lower:
                    pricing_type = "domestic"
                elif "pricing_international" in tags_lower:
                    pricing_type = "international"
        except Exception as exc:
            logging.warning("Failed to fetch pricing type for client %s: %s", client, exc)

    # 4) Update cache with final decision
    with _client_pricing_lock:
        _client_pricing_cache[client] = (pricing_type, now)

    return pricing_type

def _get_client_type(client: str | None) -> str:
    """
    get client_type either 'enterprise', 'demandpartner' from redis.

    defaults to 'enterprise' if not found.
    cached in-memory for CLIENT_PRICING_CACHE_TTL seconds.
    """
    client = (client or "").strip()
    if not client or client.lower() == "unknown":
        # Default assumption; adjust if you prefer demandpartner here
        return "enterprise"

    now = time.time()

    # 1) in-memory cache lookup
    with _client_type_lock:
        cached = _client_type_cache.get(client)
        if cached:
            cached_type, ts = cached
            if now - ts < CLIENT_PRICING_CACHE_TTL:
                return cached_type

    # 2) redis lookup
    client_type = "enterprise"
    try:
        redis_key = f"{REDIS_CLIENT_PROFILE_PREFIX}{client}"
        raw = redis_client.get(redis_key)
        if raw:
            try:
                profile = json.loads(raw)
                client_type = (profile.get("client_type") or "enterprise").strip().lower()

                # cache it
                with _client_type_lock:
                    _client_type_cache[client] = (client_type, now)

                return client_type
            except Exception as e:
                logging.warning(
                    "Failed to parse client_type from Redis client_profile for %s: %s",
                    client,
                    e,
                )
    except Exception as e:
        logging.warning("Redis error while reading client_type for %s: %s", client, e)

    return client_type


def _get_bq_client():
    """Return a BigQuery client if credentials and config are present; else None."""
    
    if not bigquery or not service_account:
        logging.error("BigQuery client failed: Google Cloud libraries not imported correctly.")
        return None

    config_ok = True
    if not BQ_PROJECT_ID:
        logging.error("BigQuery client failed: BQ_PROJECT_ID is not set. (Check 'GCP_PROJECT_ID' env var in your ConfigMap)")
        config_ok = False
    if not BQ_DATASET:
        logging.error("BigQuery client failed: BQ_DATASET is not set. (Check 'BIGQUERY_DATASET' env var in your ConfigMap)")
        config_ok = False
    if not BQ_TABLE:
        logging.error("BigQuery client failed: BQ_TABLE is not set. (Check 'BIGQUERY_TABLE' env var in your ConfigMap)")
        config_ok = False
    if not SERVICE_ACCOUNT_FILE:
        logging.error("BigQuery client failed: SERVICE_ACCOUNT_FILE is not set. (Check 'SERVICE_ACCOUNT_FILE_PATH' env var in your ConfigMap)")
        config_ok = False

    if not config_ok:
        logging.warning("BigQuery client not configured; aggregation will only be kept in-memory.")
        return None

    try:
        logging.info(f"Attempting to create BigQuery client for project '{BQ_PROJECT_ID}' with key '{SERVICE_ACCOUNT_FILE}'...")
        creds = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
        client = bigquery.Client(project=BQ_PROJECT_ID, credentials=creds)
        logging.info("Successfully initialized BigQuery client.")
        return client
    except Exception as e:
        logging.error(f"Failed to create BigQuery client from service account file: {e}")
        logging.error("This is likely an IAM PERMISSION error (check 'BigQuery Data Editor'/'BigQuery Job User' roles) or 'file not found' (check Secret mount).")
        return None



def _collapse_rows_for_drupal(rows: list[dict]) -> list[dict]:
    """
    The BigQuery rows emitted by /trigger_aggregation are *segment-based*
    (one row per transaction_type). Drupal expects one row per hour bucket.

    So we collapse rows by (datatime, carrier_name, client, customer_name, endpoint, pricing_type),
    summing est_revenue across segments and keeping the already-computed totals/avg latencies.
    """
    grouped: dict[tuple, dict] = {}
    for r in rows or []:
        key = (
            str(r.get("datatime") or "").strip(),
            str(r.get("carrier_name") or "").strip(),
            str(r.get("client") or "").strip(),
            str(r.get("customer_name") or "").strip(),
            str(r.get("endpoint") or r.get("api_path") or "").strip(),
            str(r.get("pricing_type") or "").strip(),
        )
        if key not in grouped:
            grouped[key] = dict(r)
            # start revenue sum at 0 and re-add below to be safe
            grouped[key]["est_revenue"] = 0.0
        grouped[key]["est_revenue"] = float(grouped[key].get("est_revenue") or 0.0) + float(r.get("est_revenue") or 0.0)
    # normalize rounding like your code
    for k in list(grouped.keys()):
        grouped[k]["est_revenue"] = round(float(grouped[k].get("est_revenue") or 0.0), 6)
    return list(grouped.values())


def _to_drupal_payload(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for r in rows or []:
        dt = str(r.get("datatime") or "").strip()
        api_path = str(r.get("endpoint") or r.get("api_path") or "/unknown").strip()

        # slug (only as fallback)
        attribute_slug = api_path.rstrip("/").split("/")[-1] if api_path else "unknown"

        # WHAT YOU WANT: APIAttributes from pricing.csv for this endpoint
        attribute_from_pricing = _api_attribute_for_endpoint(api_path)

        # Final attribute value sent to moriarty
        attribute_value = attribute_from_pricing or "Unknown"

        # Analytical id can still use slug (stable + short)
        hour_part = dt.replace(":", "-").replace(" ", "-")
        analytical_id = f"{(r.get('client') or 'unknown')}_{hour_part}_{attribute_slug}".replace("/", "_")

        total_full = int(r.get("total_full_rate_billable_transaction") or 0)
        total_low = int(r.get("total_lower_rate_billable_transaction") or 0)
        total_no = int(r.get("total_no_billable_transaction") or 0)

        formatted_ts = dt.replace("T", " ").replace("Z", "")
        
        out.append({
            "carrier_name": r.get("carrier_name") or "Unknown",
            "client": r.get("client") or "Unknown",
            "attribute": attribute_value,  
            "customer_name": r.get("customer_name") or "Unknown",
            "api_path": api_path,
            "analytical_id": analytical_id,
            "timestamp_interval": formatted_ts,
            "status_counts": [total_full, total_low, total_no],
            "avg_latency_full_rate": float(r.get("avg_latency_full_rate") or 0.0),
            "avg_latency_lower_rate": float(r.get("avg_latency_lower_rate") or 0.0),
            "avg_latency_no_billable": float(r.get("avg_latency_no_billable") or 0.0),
            "total_full_rate_billable_transaction": total_full,
            "total_lower_rate_billable_transaction": total_low,
            "total_no_billable_transaction": total_no,
            "est_revenue": float(r.get("est_revenue") or 0.0),
        })

    return out


def send_rows_to_drupal(rows: list[dict]) -> tuple[bool, str]:
    # Always return (ok, msg) so callers can unpack safely.
    if not rows:
        return True, "No rows to send to Drupal"

    if not DRUPAL_ANALYTICS_URL:
        msg = "DRUPAL_ANALYTICS_URL not set; skipping Drupal push"
        logging.info(msg)
        return False, msg

    if not DRUPAL_BASIC_USER or not DRUPAL_BASIC_PASS:
        msg = "DRUPAL_BASIC_USER/DRUPAL_BASIC_PASS not set; skipping Drupal push"
        logging.info(msg)
        return False, msg

    payload = _to_drupal_payload(rows)

    try:
        logging.info("Preparing to send %d analytics rows to Drupal", len(payload))
        logging.info("FULL DRUPAL PAYLOAD: %s", json.dumps(payload, indent=2))
        resp = requests.post(
            DRUPAL_ANALYTICS_URL,
            json=payload,
            auth=HTTPBasicAuth(DRUPAL_BASIC_USER, DRUPAL_BASIC_PASS),
            timeout=DRUPAL_TIMEOUT_SECONDS,
        )

        if 200 <= resp.status_code < 300:
            msg = f"Drupal push OK: status={resp.status_code} rows={len(payload)}"
            logging.info(msg)
            return True, msg

        # Non-2xx is a failure; include small body preview for debugging
        body_preview = (resp.text or "")[:1000]
        msg = f"Drupal push FAILED: status={resp.status_code} body={body_preview!r}"
        logging.warning(msg)
        return False, msg

    except Exception as e:
        logging.exception("Drupal push exception")
        return False, f"Drupal push exception: {e}"



def _update_k8s_configmap(csv_text: str) -> None:
    """
    Update the pricing ConfigMap so new pricing survives pod restarts.
    """
    cm_name = os.getenv("PRICING_CONFIGMAP_NAME")
    namespace = os.getenv("POD_NAMESPACE", "aggregates")

    if not cm_name:
        logging.info("PRICING_CONFIGMAP_NAME not set; skipping ConfigMap update")
        return

    try:
        with open("/var/run/secrets/kubernetes.io/serviceaccount/token") as f:
            token = f.read().strip()
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")

        url = f"https://{host}:{port}/api/v1/namespaces/{namespace}/configmaps/{cm_name}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/merge-patch+json",
        }

        payload = {
            "data": {
                "pricing.csv": csv_text
            }
        }

        resp = requests.patch(url, headers=headers, json=payload, verify=ca_path)
        if resp.status_code not in (200, 201):
            logging.error(
                "Failed to patch ConfigMap %s (%s): %s",
                cm_name, resp.status_code, resp.text
            )
        else:
            logging.info("Successfully updated ConfigMap %s", cm_name)
    except Exception as e:
        logging.error("Error updating ConfigMap %s: %s", cm_name, e)


def _sanitize_bq_field(name: str) -> str:
    """
    Convert CSV header to a BigQuery-safe column name:
    """
    if name is None:
        name = ""
    name = name.strip()
    name = re.sub(r"[^0-9a-zA-Z_]", "_", name)
    if not name:
        name = "field"
    # Column name cannot start with digit
    if name[0].isdigit():
        name = "_" + name
    return name


# -------------- API --------------
@app.route("/ingest", methods=["POST"])
def ingest():
    """
    Accepts either a single record or a list of records.
    Each record is expected to have a 'json' object like:
      {
        "client": "...",
        "api_path": "/camara/authorizer",
        "carrier_name": "Unknown",
        "customer_name": "Unknown",
        "status_code": 200,
        "latency_ms": 83.0,
        "datatime": "2025-11-03 10:00:00",
        "timestamp": "2025-11-03T10:56:16Z"
      }
    If 'api_path' is missing, we fallback to 'api_path_with_query' (without query part) or derive from 'uri'.
    """
    payload = request.get_json(silent=True)
    if payload is None:
        return jsonify({"error": "invalid JSON"}), 400
    records = payload if isinstance(payload, list) else [payload]

    added = 0
    with _lock:
        for rec in records:
            data = rec.get("json") if isinstance(rec, dict) else None
            if not isinstance(data, dict):
                # support direct JSON without wrapper
                data = rec if isinstance(rec, dict) else None
            if not data:
                continue

            client = (data.get("client") or "Unknown").strip() or "Unknown"
            carrier_name = (data.get("carrier_name") or "Unknown").strip() or "Unknown"
            customer_name = (data.get("customer_name") or "Unknown").strip() or "Unknown"

            # api_path detection
            api_path = data.get("api_path")
            if not api_path:
                api_path = data.get("api_path_with_query")
                if api_path and "?" in api_path:
                    api_path = api_path.split("?", 1)[0]
            if not api_path:
                # fall back to "uri" (less ideal, but present in your log)
                api_path = data.get("uri") or "/unknown"

            # time bucketing
            hour_iso = _parse_hour_bucket(data.get("datatime"), data.get("timestamp"))

            # status + latency
            status_bucket = _status_bucket(data.get("status_code"))
            try:
                latency = float(data.get("latency_ms", 0.0))  # milliseconds
            except Exception:
                latency = 0.0
                
            pricing_type = _get_client_pricing_type(client)

            key = (hour_iso, client, api_path, carrier_name, customer_name, pricing_type)
            bucket = _aggr[key]
            bucket["count"] += 1
            b = bucket[status_bucket]
            b["count"] += 1
            b["latency_sum"] += latency

    return jsonify({"ingested": added}), 200


@app.route("/debug/buffer", methods=["GET"])
def debug_buffer():
    """Return current in-memory aggregate state."""
    with _lock:
        dump = []

        for key, v in _aggr.items():
            # --- unpack key ---
            if len(key) == 6:
                hour_iso, client, api_path, carrier_name, customer_name, pricing_type = key
            else:
                hour_iso, client, api_path, carrier_name, customer_name = key
                pricing_type = DEFAULT_PRICING_TYPE

            final_carrier_name = CARRIER_NAME_OVERRIDE if CARRIER_NAME_OVERRIDE else carrier_name

            # --- totals per status ---
            total_200 = int(v["200"]["count"])
            total_404 = int(v["404"]["count"])
            total_other = int(v["other"]["count"])

            # --- per-status avg latencies ---
            avg_200 = (v["200"]["latency_sum"] / total_200) if total_200 else 0.0
            avg_404 = (v["404"]["latency_sum"] / total_404) if total_404 else 0.0
            avg_other = (v["other"]["latency_sum"] / total_other) if total_other else 0.0

            # --- unit price + attribute ---
            unit_price = _price_for_endpoint(api_path, pricing_type)
            attribute_from_pricing = _api_attribute_for_endpoint(api_path)
            attribute_value = attribute_from_pricing or "Unknown"

            # --- client_type + markup ---
            client_type = _get_client_type(client)          # 'enterprise' or 'demandpartner'
            markup_percent = _markup_for_endpoint(api_path) # e.g. 20 for 20%

            # discount  vlaues is lookup from Redis:
            # key: client_discount_price:{client}
            # value: JSON map { "<attribute_name>": <discount_pct_float>, ... }
            discount_pct = 0.0
            try:
                discount_key = f"client_discount_price:{client}"
                raw = redis_client.get(discount_key)
                if raw:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode("utf-8", errors="replace")
                    discount_map = json.loads(raw)
                    # discount is stored by attribute name
                    discount_pct = float(discount_map.get(attribute_value, 0.0) or 0.0)
            except Exception:
                logging.exception(
                    "Failed to read discount from Redis for client=%s attribute=%s",
                    client, attribute_value
                )
                discount_pct = 0.0

            # ---  markup apply rule ---
            # if demand partner -> no markup
            # if enterprise     -> apply markup
            applied_markup_pct = float(markup_percent or 0.0) if client_type == "enterprise" else 0.0

            # --- Eest revenue  formula for  calculatiion  ---
            # (($full * unit) + ($lower * unit / 2)) * (1+markup/100) * (1-discount/100)
            full_txn = float(total_200)
            lower_txn = float(total_404)
            unit_price_f = float(unit_price or 0.0)

            gross_revenue = (full_txn * unit_price_f) + (lower_txn * unit_price_f / 2.0)

            est_revenue = (
                gross_revenue
                * (1.0 + (applied_markup_pct / 100.0))
                * (1.0 - (discount_pct / 100.0))
            )

            est_revenue = round(float(est_revenue), 6)

            logging.debug(
                "debug_buffer est_revenue client=%s client_type=%s api_path=%s pricing_type=%s "
                "unit_price=%s full=%s lower=%s markup=%s discount=%s gross=%s est=%s",
                client,
                client_type,
                api_path,
                pricing_type,
                unit_price_f,
                full_txn,
                lower_txn,
                applied_markup_pct,
                discount_pct,
                gross_revenue,
                est_revenue,
            )

            dump.append({
                "datatime": hour_iso,
                "client": client,
                "client_type": client_type,
                "api_path": api_path,
                "attribute": attribute_value,
                "carrier_name": final_carrier_name,
                "customer_name": customer_name,

                # counts per status
                "total_full_rate_billable_transaction": total_200,
                "total_lower_rate_billable_transaction": total_404,
                "total_no_billable_transaction": total_other,

                # averages per status
                "avg_latency_full_rate": avg_200,
                "avg_latency_lower_rate": avg_404,
                "avg_latency_no_billable": avg_other,

                # pricing + discounts/markup + revenue
                "pricing_type": pricing_type,
                "unit_price": unit_price_f,
                "markup_percentage": applied_markup_pct,
                "discount_price": discount_pct,
                "est_revenue": est_revenue
            })
    return jsonify({"buffer_content": dump}), 200




@app.route("/trigger_aggregation", methods=["POST"])
def trigger_aggregation():
    logging.info("trigger_aggregation called")

    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            logging.debug("BQ_PROJECT_ID=%s BQ_DATASET=%s BQ_TABLE=%s", BQ_PROJECT_ID, BQ_DATASET, BQ_TABLE)
            logging.debug("DRUPAL_ANALYTICS_URL=%s", DRUPAL_ANALYTICS_URL)
        except Exception as e:
            logging.debug("Debug logging skipped: %s", e)

    with _lock:
        # snapshot current state and then clear so we don't double-insert
        snapshot = dict(_aggr)
        _aggr.clear()

    logging.info("Aggregation snapshot: groups=%d", len(snapshot))
    if logging.getLogger().isEnabledFor(logging.DEBUG):
        try:
            logging.debug("Snapshot sample keys: %s", list(snapshot.keys())[:5])
        except Exception:
            pass

    rows = []
    for key, v in snapshot.items():
        # --- unpack key ---
        if len(key) == 6:
            hour_iso, client, api_path, carrier_name, customer_name, pricing_type = key
        else:
            hour_iso, client, api_path, carrier_name, customer_name = key
            pricing_type = DEFAULT_PRICING_TYPE

        final_carrier_name = CARRIER_NAME_OVERRIDE if CARRIER_NAME_OVERRIDE else carrier_name

        # --- counts per status ---
        total_200 = int(v["200"]["count"])
        total_404 = int(v["404"]["count"])
        total_other = int(v["other"]["count"])

        # --- avg latencies per status ---
        avg_200 = (v["200"]["latency_sum"] / total_200) if total_200 else 0.0
        avg_404 = (v["404"]["latency_sum"] / total_404) if total_404 else 0.0
        avg_other = (v["other"]["latency_sum"] / total_other) if total_other else 0.0

        # --- pricing data from config/pricing table ---
        unit_price = float(_price_for_endpoint(api_path, pricing_type) or 0.0)

        attribute_from_pricing = _api_attribute_for_endpoint(api_path)
        attribute_value = attribute_from_pricing or "Unknown"
        if attribute_value == "Unknown":
            logging.debug("Skipping BQ export for endpoint %s: Attribute is Unknown", api_path)
            continue

        # --- client_type + markup% (rule controlled below) ---
        client_type = (_get_client_type(client) or "").strip().lower()          # 'enterprise' or 'demandpartner'
        markup_percent = float(_markup_for_endpoint(api_path) or 0.0)           # e.g. 10 for 10%

        # NEW RULE:
        # demandpartner -> no markup
        # enterprise    -> apply markup
        applied_markup_pct = markup_percent if client_type == "enterprise" else 0.0

        # --- discount lookup from Redis by attribute name ---
        discount_pct = 0.0
        try:
            discount_key = f"client_discount_price:{client}"
            raw = redis_client.get(discount_key)
            if raw:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="replace")
                discount_map = json.loads(raw) if raw else {}
                discount_pct = float(discount_map.get(attribute_value, 0.0) or 0.0)
        except Exception:
            logging.exception(
                "Failed to read discount from Redis for client=%s attribute=%s",
                client, attribute_value
            )
            discount_pct = 0.0

        # --- segments (keep as-is; Drupal collapse expects segments) ---
        segments = [
            ("Successful", total_200, avg_200),
            ("Unsuccessful Transactions", total_404, avg_404),
            ("Other", total_other, avg_other),
        ]

        # Precompute factors once
        markup_factor = 1.0 + (applied_markup_pct / 100.0)
        discount_factor = 1.0 - (discount_pct / 100.0)

        for tx_type, tx_count, tx_avg in segments:
            if tx_count <= 0:
                continue

            # segment gross (matches your PHP logic)
            if tx_type == "Successful":
                segment_gross = float(tx_count) * unit_price
            elif tx_type == "Unsuccessful Transactions":
                segment_gross = float(tx_count) * (unit_price / 2.0)
            else:
                segment_gross = 0.0

            # apply markup + discount to segment gross
            est_revenue = segment_gross * markup_factor * discount_factor
            est_revenue = round(float(est_revenue), 6)

            logging.debug(
                "trigger_aggregation revenue client=%s type=%s endpoint=%s unit=%s "
                "tx_type=%s tx_count=%s markup=%s discount=%s gross=%s est=%s",
                client,
                client_type,
                api_path,
                unit_price,
                tx_type,
                tx_count,
                applied_markup_pct,
                discount_pct,
                segment_gross,
                est_revenue
            )

            rows.append({
                "datatime": hour_iso,
                "carrier_name": final_carrier_name,
                "client": client,
                "client_type": client_type,
                "customer_name": customer_name,
                "endpoint": api_path,
                "attribute": attribute_value,

                # segment
                "transaction_type": tx_type,
                "transaction_type_count": tx_count,

                # totals (same for all segments)
                "total_full_rate_billable_transaction": total_200,
                "total_lower_rate_billable_transaction": total_404,
                "total_no_billable_transaction": total_other,

                # averages (same for all segments)
                "avg_latency_full_rate": avg_200,
                "avg_latency_lower_rate": avg_404,
                "avg_latency_no_billable": avg_other,

                # pricing + factors
                "pricing_type": pricing_type,
                "unit_price": unit_price,
                "markup_percentage": applied_markup_pct,
                "discount_price": discount_pct,

                # final
                "est_revenue": est_revenue
            })

    # Insert into BigQuery if configured; otherwise return what would be inserted.
    client_bq = _get_bq_client()
    if client_bq is None:
        drupal_ok, drupal_msg = send_rows_to_drupal(rows)
        return jsonify({"rows": rows, "drupal_ok": drupal_ok, "drupal_msg": drupal_msg}), 200

    if not rows:
        return jsonify({"inserted": 0}), 200

    table_id = f"{BQ_PROJECT_ID}.{BQ_DATASET}.{BQ_TABLE}"
    errors = client_bq.insert_rows_json(table_id, rows)

    if errors:
        logging.error("BigQuery insert errors: %s", errors)
        return jsonify({"inserted": 0, "errors": errors}), 500

    drupal_ok, drupal_msg = send_rows_to_drupal(rows)
    return jsonify({"inserted": len(rows), "drupal_ok": drupal_ok, "drupal_msg": drupal_msg}), 200


@app.route("/upload_pricing", methods=["POST"])
def upload_pricing():
    """
    Accept pricing.csv send by moriarty:
      - multipart/form-data with file field named 'file'
      - OR JSON body: {"csv": "<csv text>"}

    Then:
      1) rebuild in-memory _pricing_table in the same shape as _load_pricing_locked()
      2) mark pricing loaded
      3) persist to K8s ConfigMap via _update_k8s_configmap(csv_text)
    """
    global _pricing_loaded, _pricing_table

    # ---------- 1) Read CSV text ----------
    csv_text = None

    # multipart/form-data
    uploaded_file = request.files.get("file")
    if uploaded_file:
        data = uploaded_file.read()
        if not data:
            return jsonify({"error": "uploaded file is empty"}), 400
        csv_text = data.decode("utf-8", errors="replace")

    # application/json
    if not csv_text:
        payload = request.get_json(silent=True) or {}
        csv_text = payload.get("csv")
        if not csv_text:
            return jsonify({
                "error": "No CSV found. Send multipart file field 'file' or JSON {'csv': '...'}"
            }), 400

    def _safe_float(x):
        try:
            s = ("" if x is None else str(x)).strip()
            return float(s) if s != "" else None
        except Exception:
            return None

    # ---------- 2) Parse CSV and rebuild pricing table (correct structure) ----------
    table = {}
    rows_in_memory = 0
    skipped_no_endpoint = 0
    skipped_not_enabled = 0
    skipped_no_price = 0

    try:
        import csv as _csv
        import io as _io

        reader = _csv.DictReader(_io.StringIO(csv_text))
        if not reader.fieldnames:
            return jsonify({"error": "CSV has no header row"}), 400

        for row in reader:
            # Availability/enabled flags (keep consistent with your file)
            available = str(row.get("Available", "")).strip().upper()
            enabled = str(row.get("Enabled", "")).strip().upper()

            # Only load enabled rows
            if available not in ("TRUE", "YES", "1") or enabled not in ("TRUE", "YES", "1"):
                skipped_not_enabled += 1
                continue

            endpoint = (row.get("Endpoint") or row.get("endpoint") or "").strip()
            if not endpoint:
                # bundle rows with empty Endpoint are ignored for endpoint lookups
                skipped_no_endpoint += 1
                continue

            # Normalize endpoint key (match your lookup logic)
            endpoint_key = endpoint.split("?", 1)[0].rstrip("/") or "/"

            intl_price = _safe_float(row.get("InternationalPrice") or row.get("International Price"))
            dom_price = _safe_float(row.get("DomesticPrice") or row.get("Domestic Price"))

            if intl_price is None and dom_price is None:
                skipped_no_price += 1
                continue

            markup_raw = (
                row.get("MarkupPercentage")
                or row.get("Markup Percentage")
                or row.get("Markup%")
                or row.get("Markup")
                or 0
            )
            markup_percent = _safe_float(markup_raw) or 0.0

            # Your "attribute" value comes from the CSV "API" column
            api_attr = (
                row.get("API")
                or row.get("Api")
                or row.get("api")
                or row.get("API Attribute")
                or row.get("Attribute")
                or ""
            )
            api_attr = str(api_attr).strip() if api_attr is not None else ""

            #same STRUCTURE 
            table[endpoint_key] = {
                "international": intl_price,
                "domestic": dom_price,
                "markup": markup_percent,
                "api_attribute": api_attr,
            }
            rows_in_memory += 1

    except Exception as e:
        logging.exception("Failed to parse pricing CSV")
        return jsonify({"error": f"Failed to parse CSV: {e}"}), 400

    # ---------- 3) Swap pricing table atomically ----------
    with _lock:
        _pricing_table = table
        _pricing_loaded = True

    logging.info(
        "upload_pricing: loaded=%d skipped_not_enabled=%d skipped_no_endpoint=%d skipped_no_price=%d",
        rows_in_memory, skipped_not_enabled, skipped_no_endpoint, skipped_no_price
    )

    # ---------- 4) Persist to ConfigMap (your existing helper) ----------
    try:
        _update_k8s_configmap(csv_text)
    except Exception as e:
        logging.error("upload_pricing: ConfigMap update failed: %s", e)
        # Still return ok for in-memory reload, but signal CM failure
        return jsonify({
            "status": "ok",
            "pricing_rows_in_memory": rows_in_memory,
            "configmap_updated": False,
            "configmap_error": str(e),
        }), 200

    return jsonify({
        "status": "ok",
        "pricing_rows_in_memory": rows_in_memory,
        "configmap_updated": True,
    }), 200


@app.route("/debug/cache", methods=["GET"])
def debug_cache():
    """Show current client→pricing_type cache and TTL remaining."""
    now = time.time()
    out = []

    with _client_pricing_lock:
        for client, (ptype, ts) in _client_pricing_cache.items():
            ttl_left = CLIENT_PRICING_CACHE_TTL - (now - ts)
            out.append({
                "client": client,
                "pricing_type": ptype,
                "cached_at": ts,
                "ttl_remaining_seconds": max(0, round(ttl_left, 2))
            })

    return jsonify({"cache": out}), 200


# @app.route("/", methods=["GET"])
# def health():
#     return jsonify({"status": "ok"}), 200

@app.route("/healthz", methods=["GET"])
def healthz():
    """Simple health endpoint."""
    return jsonify({"status": "ok"}), 200


@app.route("/client_profile", methods=["POST"])
def client_profile():
    # --- get the req body fromm moriarty ---
    try:
        body = request.get_json(silent=True) or {}
        logging.debug(
            "Incoming /client_profile request body:\n%s",
            json.dumps(body, indent=2)
        )
    except Exception as e:
        logging.exception("Error parsing JSON in client_profile")
        return jsonify({"error": "invalid_json", "details": str(e)}), 400

    # --- get required fields ---
    try:
        client = (body.get("client") or "").strip()
        client_type = (body.get("client_type") or "").strip().lower()
        status = (body.get("status") or "").strip().lower()
        pricing_type = (body.get("pricing_type") or "").strip().lower()
        discount_prices_raw = body.get("discount_price") or []
    except Exception as e:
        logging.exception("Error extracting fields in client_profile")
        return jsonify({"error": "invalid_fields", "details": str(e)}), 400

    # --- validating  ---
    if not client:
        return jsonify({"error": "client is required"}), 400

    if client_type not in ("demandpartner", "enterprise"):
        return jsonify({"error": "client_type must be 'demandpartner' or 'enterprise'"}), 400

    if status not in ("active", "inactive"):
        return jsonify({"error": "status must be 'active' or 'inactive'"}), 400

    if pricing_type not in ("domestic", "international"):
        return jsonify({"error": "pricing_type must be 'domestic' or 'international'"}), 400

    # --- building client profile ---
    profile = {
        "client": client,
        "client_type": client_type,
        "status": status,
        "pricing_type": pricing_type,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }

    # --- storing profile in Redis ---
    profile_key = f"client_profile:{client}"
    try:
        redis_client.set(profile_key, json.dumps(profile))
        logging.info(
            "Stored client profile for client=%s key=%s",
            client, profile_key
        )
    except Exception as e:
        logging.exception(
            "Redis error storing client profile for client=%s", client
        )
        return jsonify({"error": "redis_store_failed"}), 500

    # --- processing discount prices ---
    discount_price_map = {}

    try:
        for item in discount_prices_raw:
            if not isinstance(item, dict):
                continue

            attr = (item.get("attribute_name") or "").strip()
            price_raw = (item.get("discount_price") or "").strip()

            if not attr:
                continue

            try:
                price = float(price_raw)
            except ValueError:
                logging.warning(
                    "Invalid discount_price for client=%s attribute=%s value=%s",
                    client, attr, price_raw
                )
                continue

            discount_price_map[attr] = price

    except Exception as e:
        logging.exception(
            "Error processing discount_price for client=%s", client
        )
        return jsonify({"error": "invalid_discount_price"}), 400

    # --- storing discount prices in Redis ---
    discount_key = f"client_discount_price:{client}"
    try:
        if discount_price_map:
            redis_client.set(discount_key, json.dumps(discount_price_map))
            logging.info(
                "Stored %d discount prices for client=%s key=%s",
                len(discount_price_map), client, discount_key
            )
    except Exception as e:
        logging.exception(
            "Redis error storing discount prices for client=%s", client
        )
        return jsonify({"error": "redis_discount_store_failed"}), 500

    # --- updating local pricing cache ---
    try:
        now = time.time()
        with _client_pricing_lock:
            _client_pricing_cache[client] = (pricing_type, now)
    except Exception as e:
        logging.warning(
            "Failed to update local pricing cache for client=%s: %s",
            client, e
        )

    return jsonify({
        "status": "ok",
        "profile": profile,
        "discount_price_count": len(discount_price_map),
    }), 200



if __name__ == "__main__":
    # Run Flask app
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))