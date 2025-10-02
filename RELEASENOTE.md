# Release v1.0.0

## Overview
This release marks a major milestone with full API coverage and enhanced security features.

## Highlights
- All **47 endpoints** are included and fully functional.  
- **OAuth 2.0 enabled** across endpoints for secure access.  
- **Camera Authentication** service integrated.  
- Added **TS43 1st method** for authentication and validation.  

**Image & Tag:**  
```
us-central1-docker.pkg.dev/sherlock-004/ts43/camera-auth:v1.0.0
```

---

# Release v2.0.0

## Overview
This release enhances Camera Authentication with two fully working endpoints and integrates with Kong + JWT issuer for end-to-end token orchestration.

## Highlights
- All **47 endpoints** remain included and fully functional.  
- **OAuth 2.0 enabled** across endpoints for secure access.  
- **Camera Authentication** upgraded:  
  - Integrated with external authentication service (`EXTERNAL_AUTH_URL`) to fetch verified **msisdn**.  
  - **Auth code** generation now based on externally resolved `msisdn`.  
  - Context stored in **Redis** and validated for one-time use.  
  - Token exchange flow enhanced to issue a final **JWT token** via `ISSUE_JWT_URL`.  
- Added support for **x-correlator** header passthrough in external auth requests.  
- Improved **logging & error handling** for external calls, Redis, and Kong Admin API.  

**Image & Tag:**  
```
us-central1-docker.pkg.dev/sherlock-004/ts43/camera-auth:v2.0.0
```

## Camera Authentication Usage

### Step 1: Authorize
```bash
curl --location 'https://34.61.21.100:32443/camera/authorizer' --header 'Content-Type: application/x-www-form-urlencoded' --data-urlencode 'response_type=code' --data-urlencode 'client_id=test' --data-urlencode 'redirect_uri=https://oauth.pstmn.io/v1/callback' --data-urlencode 'scope=sherlockapiresource/write' --data-urlencode 'provision_key=OAuth-Token-Dispenser-Key' --data-urlencode 'authenticated_userid=test_id' --data-urlencode 'grant_type=authorization_code' --data-urlencode 'login_hint=4251000000' --data-urlencode 'identifier=ABC12345' --data-urlencode 'carrierName=lab' --data-urlencode 'customerName=Bank1' --data-urlencode 'ipAddress=192.168.0.1'
```

### Step 2: Exchange Code for Token
```bash
curl --location --request POST 'https://34.61.21.100:32443/camera/token' --header 'Content-Type: application/x-www-form-urlencoded' --header 'code: dG9rZW49NDI1MTAwMDAwMDoxNzU3NDMyOTAwJmxvZ2luX2hpbnQ9NDI1MTAwMDAwMA=='
```

---
# Release v2.0.1
now provides a **verified msisdn-driven flow**, Redis-backed one-time auth codes, and secure JWT token issuance with full Kong integration.
Image : us-central1-docker.pkg.dev/sherlock-004/ts43/camera-auth
Tag :  v2.0.3

### Step 1: Authorize
```bash
curl --location 'https://34.61.21.100:32443/camera/authorizer' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--header 'x-correlator: 123-456-789' \
--data-urlencode 'response_type=code' \
--data-urlencode 'client_id=test' \
--data-urlencode 'redirect_uri=https://oauth.pstmn.io/v1/callback' \
--data-urlencode 'scope=sherlockapiresource/write' \
--data-urlencode 'provision_key=OAuth-Token-Dispenser-Key' \
--data-urlencode 'authenticated_userid=test_id' \
--data-urlencode 'grant_type=authorization_code' \
--data-urlencode 'customerName=Bank1' \
--data-urlencode 'ipAddress=192.168.0.1'
```


### Step 2: Exchange Code for Token
```bash
curl --location --request POST 'https://34.61.21.100:32443/camera/token' \
--header 'Content-Type: application/x-www-form-urlencoded' \
--header 'code: dG9rZW49KzE0MjUxMDAwMDAwOjE3NTg3MzgyNTcmbG9naW5faGludD0rMTQyNTEwMDAwMDA='
```

### Step 3: Verify Phone Number
```bash
curl --location 'https://34.61.21.100:32443/number-verification/v0/verify_phone_number' \
--header 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJ0ZXN0LWFwcC1pc3N1ZXIiLCJsb2dpbl9oaW50IjoiKzE0MjUxMDAwMDAwIiwiaWF0IjoxNzU4NzM3OTQ3LCJleHAiOjE3NTg3NDE1NDd9.gc-UoqPaUDyYkJ4bwdPHGm7gq_wbo1YKSMFAtfz0veg' \
--header 'customerName: Bank1' \
--header 'x-correlator: 123-456-789' \
--header 'Content-Type: application/json' \
--data '{
  "phoneNumber": "+14251000000"
}'
```

### camera auth v20.5.2
  Added loging info