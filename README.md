Kong Gateway (Enterprise) + Postgres on Kubernetes (K3s/Rancher HelmController)

This repo installs Kong Gateway Enterprise backed by Postgres and exposes:

Kong Is Exposed on HTTP and HTTPS Ports
Proxy: 32080 (HTTP), 32443 (HTTPS)
  TS 43 Plugin connect to KONG via HTTPS but certificate will be verified at KONG VERIFY_TLS @ts43-issue-auth-code.py line number 19.

Admin API: 32081 (HTTP), 32441 (HTTPS)

Manager UI: 30516 (HTTP), 30952 (HTTPS)

It also includes an overlay to set environment-specific admin_gui_api_url and admin_gui_url without hard-coding IPs in the base config.

Prerequisites

kubectl configured for your cluster (K3s recommended).

Rancher Helm Controller CRDs available (helm.cattle.io/v1).

Namespace:

kubectl create ns kong


Kong Enterprise license.json file (do not commit it to git).

1) Install Postgres (Bitnami)
# From repo root
kubectl apply -n kong -k apps/postgres/on-perm

# Wait for Postgres
kubectl -n kong rollout status statefulset/kong-pg-postgresql --timeout=5m

# Inspect
kubectl -n kong get pods,svc -l app.kubernetes.io/name=postgresql

Quick DB test
kubectl -n kong run psql-client --rm -it --image=postgres:15 --restart=Never \
  --env=PGPASSWORD=supersecret-kong -- \
  psql "host=postgres-kong.kong.svc.cluster.local port=5432 dbname=kong user=kong" \
  -c "select now(), current_user, current_database();"

# Add the Enterprise License

From the folder containing license.json:
cd kong-k8-installation
kubectl -n kong create secret generic kong-enterprise-license \
  --from-file=license=./license.json

check the value of secret:
kubectl -n kong create secret generic kong-enterprise-license \
  --from-file=license=./license.json

# Install Kong Gateway Enterprise

In the folder containing apps/kong/on-perm/deployment-patch.yaml (HelmChart) configures:
  1. Enterprise image kong/kong-gateway
  2. External Postgres connection
  3. NodePorts for Proxy/Admin/Manager
  4. Migrations enabled

Add the change the helm configuration here:
  1. pg_host
  2. admin_gui_url
  3. admin_gui_api_url

Apply:
kubectl apply -n kong -k apps/kong/on-perm

CHeck for Ports 
[ansible@kong-onperm-cluster-01 kong-k8-installation]$ kubectl -n kong get svc
NAME                           TYPE        CLUSTER-IP     EXTERNAL-IP   PORT(S)                         AGE
kong-kong-admin                NodePort    10.43.46.61    <none>        8001:32081/TCP,8444:32441/TCP   3d10h
kong-kong-manager              NodePort    10.43.149.20   <none>        8002:30516/TCP,8445:30952/TCP   3d10h
kong-kong-metrics              ClusterIP   10.43.190.71   <none>        10255/TCP,10254/TCP             3d10h
kong-kong-proxy                NodePort    10.43.239.88   <none>        80:32080/TCP,443:32443/TCP      3d10h
kong-kong-validation-webhook   ClusterIP   10.43.4.204    <none>        443/TCP                         3d10h
kong-pg-postgresql             ClusterIP   10.43.40.105   <none>        5432/TCP                        3d11h
kong-pg-postgresql-hl          ClusterIP   None           <none>        5432/TCP                        3d11h
ts43-auth-backend              ClusterIP   10.43.95.105   <none>        80/TCP                          2d10h

Check the status of pods
[ansible@kong-onperm-cluster-01 kong-k8-installation]$ kubectl get pods -n kong
NAME                           READY   STATUS      RESTARTS   AGE
helm-install-kong-g4kx6        0/1     Completed   0          7h18m
helm-install-kong-pg-gtpgq     0/1     Completed   0          3d12h
kong-ee-bootstrap-once-k6h98   0/1     Completed   0          110m
kong-kong-5b5c7cd4c8-pnqxr     2/2     Running     0          80m
kong-pg-postgresql-0           1/1     Running     0          3d12h
ts43-auth-86bbf4f95f-2q8dl     1/1     Running     0          9h
ts43-auth-86bbf4f95f-84qj8     1/1     Running     0          9h
ts43-redis-0                   1/1     Running     0          2d11h

check kong version:
 kubectl -n kong exec deploy/kong-kong -c proxy -- kong version

 check Kong Licenses:
 kubectl -n kong exec deploy/kong-kong -c proxy -- printenv KONG_LICENSE_DATA | head -c 120; echo

# Manager Ednpoint
curl -I http://<NODE_PUBLIC_IP>:30516/workspaces

# Proxy Endpoint
curl -I https://<NODE_PUBLIC_IP>:32080/



# Deploy Redis
kubectl apply -k ts43-redis/k8s/on-perm
kubectl -n kong get pods,svc | grep ts43-redis

# docker build and push cookie-generator-service  Image to sherlock-004:
cd kong-k8-installation/services/cookie-generator-service/app

sudo docker buildx build \
  --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/sherlock-004/ts43/cookie-generator-service:v8 \
  --push .

cd kong-k8-installation
kubectl apply -k services/cookie-generator-service/k8s/on-perm



# docker build and push TS43 Authe code Image to sherlock-004:
cd kong-k8-installation/services/ts43-auth/app

sudo docker buildx build \
  --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/sherlock-004/ts43/ts43-authcode:v10 \
  --push .

# Deploy TS43 AUth Code  Image
cd kong-k8-installation
kubectl apply -k services/ts43-auth/k8s/on-perm
kubectl -n kong get deploy,po,svc | grep ts43-auth


# docker build and push Camera  Image to sherlock-004:
cd kong-k8-installation/services/camera-auth/app

sudo docker buildx build \
  --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/sherlock-004/ts43/camera-auth:v12 \
  --push .

# Deploy Camera Image
cd kong-k8-installation
kubectl apply -k services/camera-auth/k8s/on-perm
kubectl -n kong get deploy,po,svc | grep camera-auth


# docker build and push JWT Issuer to sherlock-004:
cd kong-k8-installation/services/jwt-issuer/app

sudo docker buildx build \
  --platform linux/amd64 \
  -t us-central1-docker.pkg.dev/sherlock-004/ts43/jwt-issuer:v5 \
  --push .

# Deploy jwt-issuer

1. Create Fallback secret:
  kubectl create secret generic -n kong jwt-issuer-secret --from-literal=secret=strongpassword
2. Deployment
cd kong-k8-installation
kubectl apply -k services/jwt-issuer/k8s/on-perm
kubectl -n kong get deploy,po,svc | grep jwt-issuer



Deploy TS 43 Endpoint to KONG:
# dry-run
helm upgrade --install ts43-config ./charts/ts43-config -n kong --debug --dry-run

# apply & wait
helm upgrade --install ts43-config ./charts/Sherlock -n kong 

# In the KONG UI , for gateway service and route
  http://34.61.21.100:30516/default/services
  http://34.61.21.100:30516/default/routes






# TOOLS:
1. Kong runtime log:
    kubectl -n kong exec -it deploy/kong-kong -c proxy -- sh
    cat /tmp/kong_requests.log

2 rolling restart kong deployment
     kubectl -n kong rollout restart deployment kong-kong

3 COnvert OPENAPI to Kong File
     deck file openapi2kong --spec openapi.json --output-file sherlock.kong.yaml



#troubleshoot:
Why kong shows OSS version not Enterprise:
 kubectl -n kong get helmchart.helm.cattle.io kong -o yaml | sed -n '1,160p'
    There is no image: or enterprise: keys in .spec.valuesContent, so its dfault to OSS free version. 



Incase of License ISSUE:
check kong ENV
1.  kubectl exec -n kong  kong-kong-5476b9f9d6-tkhfh -c proxy -- printenv | grep KONG
2. KONG_LICENSE_DATA // this should hold full license.


Run Kong In Debug Mode:
1. kubectl set env deploy/kong-kong KONG_LOG_LEVEL=debug -n kong
2. kubectl rollout restart deployment kong-kong -n kong
3. kubectl rollout status deploy/kong-kong -n kong 
4. kubectl logs deploy/kong-kong -c proxy -f -n kong 