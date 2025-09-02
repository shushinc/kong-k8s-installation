import json
import os
import boto3
import logging
import uuid
import redis
import requests
import base64
import time
import hmac
import hashlib
from botocore.exceptions import ClientError
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from Crypto.Cipher import AES
from datetime import datetime, timedelta
import urllib.parse

# Set up logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Ensure the key is exactly 32 bytes
SECRET_KEY = os.getenv("SECRET_KEY", "0123456789abcdef0123456789abcdef")
SECRET_KEY = SECRET_KEY.ljust(32, "0")[:32].encode()

# Load environment variables
REDIS_HOST = os.getenv("REDIS_HOST")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6380))
REDIS_TTL = int(os.getenv("REDIS_TTL", 300))
USER_POOL_ID = os.getenv("USER_POOL_ID")
AWS_REGION = os.getenv("AWS_REGION", boto3.Session().region_name)
COGNITO_DOMAIN = os.getenv("COGNITO_DOMAIN")
SECRET_KEY = os.getenv("SECRET_KEY", "your-secure-key")  # Store securely
USERNAME = os.getenv("USERNAME", "authserveruser") 
PASSWORD = os.getenv("PASSWORD", "Authserveruser@123.") 


# Validate required environment variables
required_env_vars = {
    "REDIS_HOST": REDIS_HOST,
    "USER_POOL_ID": USER_POOL_ID,
    "AWS_REGION": AWS_REGION,
    "COGNITO_DOMAIN": COGNITO_DOMAIN
}

missing_vars = [key for key, value in required_env_vars.items() if not value]
if missing_vars:
    logger.error(f"Missing required environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing required environment variables: {', '.join(missing_vars)}")

# Redis connection (singleton pattern)
_redis_client = None


def pad(data):
    """PKCS7 padding to make the data length a multiple of 16 bytes."""
    pad_length = 16 - len(data) % 16
    return data + (chr(pad_length) * pad_length).encode()


def get_redis_client():
    """Returns a Redis connection, reusing an existing one if available."""
    global _redis_client
    if _redis_client is None:
        _redis_client = redis.StrictRedis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    return _redis_client

#Just base64-encoded
def generate_auth_code(login_hint):
    """Generate a base64-encoded auth code from login_hint + timestamp."""
    timestamp = str(int(time.time()))  # Get current timestamp (epoch time)
    raw_data = f"{login_hint}:{timestamp}"  # Concatenate login_hint and timestamp

    auth_code = base64.urlsafe_b64encode(raw_data.encode()).decode()  # Encode to Base64 (URL-safe)
    return auth_code


def store_auth_code(msisdn, auth_code):
    """Store auth code and phone number in Redis with an expiry time."""
    try:
        redis_client = get_redis_client()
        logger.info(f"Storing auth code in Redis: msisdn={msisdn}, auth_code={auth_code[:4]}****")

        redis_client.setex(f"auth_code:{auth_code}", REDIS_TTL, msisdn)
        logger.info("Auth code successfully stored in Redis.")
        return True
    except redis.RedisError as e:
        logger.error(f"Redis error: {str(e)}")
        return False

def get_msisdn_from_auth_code(auth_code):
    """Retrieve the MSISDN (phone number) associated with the auth code from Redis."""
    try:
        redis_client = get_redis_client()
        msisdn = redis_client.get(f"auth_code:{auth_code}")
        if msisdn:
            redis_client.delete(f"auth_code:{auth_code}")  # Delete after retrieval
        return msisdn
    except redis.RedisError as e:
        logger.error(f"Redis error: {str(e)}")
        return None
    
def get_client_secret(client_id):
    """Retrieve client secret from Cognito based on client_id."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        response = client.describe_user_pool_client(UserPoolId=USER_POOL_ID, ClientId=client_id)
        
        client_details = response.get("UserPoolClient", {})
        client_secret = client_details.get("ClientSecret")

        if not client_secret:
            logger.error(f"Client secret not found for client_id={client_id}")
            return None
        
        return client_secret
    except ClientError as e:
        logger.error(f"Cognito error while fetching client_secret: {str(e)}")
        return None


def validate_client(client_id, redirect_uri, scope):
    """Validate client details against Cognito User Pool."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        response = client.describe_user_pool_client(UserPoolId=USER_POOL_ID, ClientId=client_id)
        client_details = response.get("UserPoolClient", {})

        if not client_details:
            logger.error(f"Client {client_id} not found in Cognito.")
            return False

        if redirect_uri not in client_details.get("CallbackURLs", []):
            logger.error(f"Invalid redirect URI: {redirect_uri}. Expected: {client_details.get('CallbackURLs', [])}")
            return False

        if scope not in client_details.get("AllowedOAuthScopes", []):
            logger.error(f"Invalid scope: {scope}. Expected: {client_details.get('AllowedOAuthScopes', [])}")
            return False

        return True
    except ClientError as e:
        logger.error(f"Cognito ClientError: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"Validation error: {str(e)}")
        return False

# Set up a requests session with retry logic
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
session.mount("http://", HTTPAdapter(max_retries=retries))


def fetch_auth_service(auth_url):
    """Fetch authentication service response with retry logic and timeout."""
    try:
        logger.info(f"Fetching auth service: {auth_url}")
        response = session.get(auth_url, timeout=3)  # Reduce timeout to 3 seconds
        return response
    except requests.Timeout:
        logger.error("Auth service timeout!")
        return None
    except requests.RequestException as e:
        logger.error(f"Auth service request error: {str(e)}")
        return None

def calculate_secret_hash(client_id, client_secret, username):
    """Generate the Cognito secret hash using HMAC-SHA256"""
    message = username + client_id
    digest = hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()

# Function to set a custom attribute (login_hint) before authentication
def set_user_attribute(username, login_hint):
    """Store login_hint in Cognito user attributes before authentication."""
    try:
        client = boto3.client("cognito-idp", region_name=AWS_REGION)
        response = client.admin_update_user_attributes(
            UserPoolId=USER_POOL_ID,
            Username=username,
            UserAttributes=[
                {
                    "Name": "custom:login_hint",
                    "Value": login_hint
                }
            ]
        )
        logger.info(f"User attribute 'custom:login_hint' updated for {username}: {login_hint}")
        return True
    except ClientError as e:
        logger.error(f"Error updating user attributes: {str(e)}")
        return False
    
#jwt_token = get_jwt_token(client_id, client_secret, scope, login_hint, USERNAME, PASSWORD)
#jwt_token = get_jwt_token(client_id, client_secret, scope, login_hint, USERNAME, PASSWORD)
def get_jwt_token(client_id, client_secret,scope,login_hint,username, password):
    """Obtain a JWT token by handling NEW_PASSWORD_REQUIRED challenge."""
    client = boto3.client('cognito-idp')
    secret_hash = calculate_secret_hash(client_id, client_secret, username)
    # Store login_hint as a custom attribute
    logger.info(f"secret_hash {secret_hash}")
    set_user_attribute(username, login_hint)
    try:
        # Initial authentication attempt
        auth_response = client.admin_initiate_auth(
        # auth_response = client.initiate_auth(
            UserPoolId=USER_POOL_ID,
            ClientId=client_id,
            AuthFlow='ADMIN_NO_SRP_AUTH',
            # AuthFlow='USER_PASSWORD_AUTH',
            # AuthFlow='CUSTOM_AUTH',
            AuthParameters={
                'USERNAME' : username,
                'PASSWORD' : password,
                'SECRET_HASH' : secret_hash,
                'login_hint' : "login_hint"     
            },
            ClientMetadata={  # Pass metadata to Lambda trigger
                "login_hint": "123123",
                'custom_context': 'value_here'
            }
            # ClientMetadata={  # Pass metadata to Lambda trigger
            #     'login_hint': login_hint
            # },
        )
        logger.info(f"auth_response: {auth_response}")
        # Check if password reset is required - Elango Change
        # if auth_response.get('ChallengeName') == 'NEW_PASSWORD_REQUIRED':
        #     logger.info("Resetting password for user...")
        #     # Set a new password (retrieve from secure storage or environment)
        #     new_password = os.getenv("RESET_PASSWORD", "SecurePass123!")  # Store securely
        #     # Respond to the challenge
        #     challenge_response = client.admin_respond_to_auth_challenge(
        #         UserPoolId=USER_POOL_ID,
        #         ClientId=client_id,
        #         ChallengeName='NEW_PASSWORD_REQUIRED',
        #         ChallengeResponses={
        #             'USERNAME': username,
        #             'NEW_PASSWORD': new_password,
        #             'SECRET_HASH': secret_hash
        #         },
        #         Session=auth_response['Session']
        #     )
        #     return challenge_response.get('AuthenticationResult')
            # Check if password reset is required - Elango Change
        return auth_response.get('AuthenticationResult')
    except ClientError as e:
        logger.error(f"Cognito error: {e.response['Error']['Message']}")
        return None

def extract_token_info(jwt_response):
    """Extracts only the required fields from the JWT response."""
    try:
        token_info = {
            "AccessToken": jwt_response.get("AccessToken"),
            "ExpiresIn": jwt_response.get("ExpiresIn"),
            "TokenType": jwt_response.get("TokenType"),
        }
        return token_info
    except Exception as e:
        logger.error(f"Error extracting token info: {str(e)}")
        return {"error": "Failed to extract token details"}


def decode_auth_code(auth_code):
    """Decode a base64-encoded auth code to get login_hint and timestamp."""
    try:
        if not auth_code:
            logger.error("decode_auth_code: Received empty or None auth_code")
            return None, None  # Handle empty auth codes

        logger.info(f"decode_auth_code: Decoding auth_code={auth_code[:6]}****")

        raw_data = base64.urlsafe_b64decode(auth_code).decode()  # Decode from Base64
        if ":" not in raw_data:
            logger.error(f"decode_auth_code: Invalid format, missing ':' in {raw_data}")
            return None, None  # Ensure format is correct

        login_hint, timestamp = raw_data.split(":")
        logger.info(f"decode_auth_code: Decoded login_hint={login_hint}, timestamp={timestamp}")
        return login_hint, timestamp
    except Exception as e:
        logger.error(f"decode_auth_code: Failed to decode auth_code - {str(e)}")
        return None, None  # Invalid or corrupted code

def is_auth_code_valid(auth_code):
    """Decode auth code and check if it's valid (not expired)."""
    if not auth_code:
        logger.error("is_auth_code_valid: Auth code is missing")
        return None, "Invalid token"

    login_hint, timestamp = decode_auth_code(auth_code)
    if not login_hint or not timestamp:
        logger.error("is_auth_code_valid: Decoded auth code is invalid")
        return None, "Invalid token"

    try:
        auth_time = datetime.utcfromtimestamp(int(timestamp))  
        expiry_time = auth_time + timedelta(seconds=REDIS_TTL)  # Use REDIS_TTL instead of TOKEN_EXPIRY_SECONDS
        current_time = datetime.utcnow()

        if current_time > expiry_time:
            logger.warning("is_auth_code_valid: Auth code has expired")
            return None, "Token is expired"

        return login_hint, None  # Token is valid
    except ValueError as e:
        logger.error(f"is_auth_code_valid: Invalid timestamp in auth_code - {str(e)}")
        return None, "Invalid token"



def lambda_handler(event, context):
    """Main Lambda function entry point."""
    try:
        path = event.get("path", "")
        headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
        query_params = event.get("queryStringParameters", {}) or {}

        if path == "/authorizer":
            logger.info("Authorizer request received")
            client_id = headers.get("client_id") or query_params.get("client_id")
            redirect_uri = headers.get("redirect_uri") or query_params.get("redirect_uri")
            scope = headers.get("scope") or query_params.get("scope")
            login_hint = headers.get("login_hint") or query_params.get("login_hint")

            logger.info(f"Authorizer request received: client_id={client_id[:4]}****, login_hint=****")
            # Check for missing parameters
            missing_params = []
            if not client_id:
                missing_params.append("client_id")
            if not redirect_uri:
                missing_params.append("redirect_uri")
            if not scope:
                missing_params.append("scope")
            # if not login_hint:
            #     missing_params.append("login_hint")

            if missing_params:
                logger.error(f"Missing required parameters: {', '.join(missing_params)}")
                return {"statusCode": 400, "body": json.dumps({"error": f"Missing parameters: {', '.join(missing_params)}"})}
            
            if not all([client_id, redirect_uri, scope, login_hint]):
                return {"statusCode": 400, "body": json.dumps({"error": "Missing required parameters"})}

            if not validate_client(client_id, redirect_uri, scope):
                return {"statusCode": 400, "body": json.dumps({"error": "Invalid client credentials"})}

            # If login_hint is missing `+`, assume it was stripped
            # logger.info(f"Before login_hint: {login_hint}")
            # if login_hint and not login_hint.startswith("+"):
            #     login_hint = f"+{login_hint.strip()}"

            # login_hint = urllib.parse.quote_plus(login_hint).replace("+", "%2B")

            logger.info(f"Before login_hint: {login_hint}")
            if login_hint.startswith("+"):  # Ensure encoding only if '+' exists
                    logger.info(f"Enter into +")
                    login_hint = login_hint.replace("+", "%2B")  # Replace '+' with '%2B'
                
            auth_url = f"http://34.54.169.57/v0/authenticate_user?msisdn={login_hint}"
            logger.info(f"Calling authentication service: {auth_url}")
            logger.info(f"After login_hint: {login_hint}")
            
            # response = fetch_auth_service(auth_url)
            
            try:
                response = fetch_auth_service(auth_url)
            except ValueError as e:
                # Handle 404 or other expected validation errors
                logger.error(f"Validation error: {str(e)}")
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": str(e)})
                }
            except Exception as e:
                # Catch unexpected errors
                logger.error(f"Unexpected error in /authorizer: {str(e)}", exc_info=True)
                return {
                    "statusCode": 500,
                    "body": json.dumps({"error": "Internal server error"})
                }

            logger.info(f"Auth service response: {response.status_code}, body: {response.text}")
              
            if response.status_code == 200:
                auth_code = generate_auth_code(login_hint)
                # if store_auth_code(login_hint, auth_code):
                return {"statusCode": 302, "headers": {"Location": f"{redirect_uri}?code={auth_code}"}}
                # else:
                #     return {"statusCode": 500, "body": json.dumps({"error": "Failed to store auth code"})}
            elif response.status_code == 404:
                logger.error("Phone number not found")
                return {"statusCode": 404, "body": json.dumps({"error": "Phone number not found"})}
            else:
                logger.error(f"Auth service returned unexpected status: {response.status_code}")
                return {"statusCode": 400, "body": json.dumps({"error": "Phone number validation failed"})}

            # if response.status_code == 200:
            #     auth_code = generate_auth_code(login_hint)
            #     if store_auth_code(login_hint, auth_code):
            #         return {"statusCode": 302, "headers": {"Location": f"{redirect_uri}?code={auth_code}"}}
            #     else:
            #         return {"statusCode": 500, "body": json.dumps({"error": "Failed to store auth code"})}
            # elif response.status_code == 404:
            #     logger.error("MSISDN not found in authentication service")
            #     return {"statusCode": 404, "body": json.dumps({"error": "Phone number not found"})}
            # else:
            #     logger.error(f"Auth service returned unexpected status: {response.status_code}")
            #     return {"statusCode": 400, "body": json.dumps({"error": "Phone number validation failed"})}

            # if response and response.status_code == 200:
            #     logger.info(f"Auth service response: {response.status_code}, body: {response.text}")
            #     auth_code = generate_auth_code(login_hint)
            #     # if store_auth_code(login_hint, auth_code):
            #     return {"statusCode": 302, "headers": {"Location": f"{redirect_uri}?code={auth_code}"}}
            # return {"statusCode": 400, "body": json.dumps({"error": "Phone number not validated"})}

        elif path == "/token":
            logger.info(f"Token request received")

            client_id = headers.get("client_id") or query_params.get("client_id")
            redirect_uri = headers.get("redirect_uri") or query_params.get("redirect_uri")
            scope = headers.get("scope") or query_params.get("scope")
            login_hint = headers.get("login_hint") or query_params.get("login_hint")
            auth_code = headers.get("code") or query_params.get("code")

            logger.info(f"Token request received: client_id={client_id[:4]}****, auth_code={auth_code[:4]}****")
            # Check for missing parameters

            # Identify missing parameters
            missing_params = []
            if not client_id:
                missing_params.append("client_id")
            # if not login_hint:
            #     missing_params.append("login_hint")
            if not scope:
                missing_params.append("scope")
            if not auth_code:
                missing_params.append("code")
                
            # If any required parameter is missing or null, exit immediately
            if missing_params:
                error_message = f"Missing parameters: {', '.join(missing_params)}"
                logger.error(error_message)
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": error_message}),
                    "headers": {"Content-Type": "application/json"},
                }
            logger.info(f"Token request received: client_id={client_id[:4]}****, auth_code=****")

            # if not client_id or not auth_code:
            #     return {
            #         "statusCode": 400,
            #         "body": json.dumps({"error": "Missing client_id or auth_code"}),
            #         "headers": {"Content-Type": "application/json"}
            #     }

            try:
                # Retrieve client secret from Cognito
                client_secret = get_client_secret(client_id)
                if not client_secret:
                    return {
                        "statusCode": 400,
                        "body": json.dumps({"error": "Invalid client_id or missing client_secret"}),
                        "headers": {"Content-Type": "application/json"},
                    }

                # Validate auth_code
                login_hint, error_msg = is_auth_code_valid(auth_code)
                if error_msg:
                    return {
                        "statusCode": 400,
                        "body": json.dumps({"error": error_msg}),
                        "headers": {"Content-Type": "application/json"},
                    }

                logger.info(f"Token request: Auth code validated successfully for login_hint={login_hint}")

                # Get JWT token from Cognito
                jwt_token = get_jwt_token(client_id, client_secret, scope, login_hint, USERNAME, PASSWORD)
                
                if jwt_token:
                    token_info = extract_token_info(jwt_token)
                    return {
                        "statusCode": 200,
                        "body": json.dumps(token_info),
                        "headers": {"Content-Type": "application/json"},
                    }

                return {
                    "statusCode": 500,
                    "body": json.dumps({"error": "Failed to retrieve JWT token"}),
                    "headers": {"Content-Type": "application/json"},
                }

            except Exception as e:
                logger.error(f"Unexpected error in /token: {str(e)}", exc_info=True)
                return {
                    "statusCode": 500,
                    "body": json.dumps({"error": "Internal server error"}),
                    "headers": {"Content-Type": "application/json"},
                }

        return {"statusCode": 404, "body": json.dumps({"error": "Invalid endpoint"})}

    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": "Internal server error"})}

