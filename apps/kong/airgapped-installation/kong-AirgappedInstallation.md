
Add Kong Images of the all agent nodes
download the folder:https://drive.google.com/drive/folders/1R-UlQrehHwA7rFQcavg5DdPlZ3RbGtDS
Copy Below Images to Worker Nodes:

cp ./apps/kong/airgapped-installation/image-bundles/kong-airgap-images.tar /var/lib/rancher/k3s/
tar -xvf kong-airgap-images.tar

Copy Below Images to Master and worker Nodes:
cp ./apps/kong/airgapped-installation/image-bundles/ingress-controller.tar /var/lib/rancher/k3s/agent/images/


vi apps/kong/base/application.yaml 
    change :
        admin_gui_url: "http://<masternode-ip>:30516"
        admin_gui_api_url: "http://<masternode-ip>:32081"
kubectl apply -n kong -k apps/kong/on-perm
check the kong services:
    kubectl get svc -n kong

Install HELM :
# Move the binary to the correct location

tar -xzvf apps/kong/airgapped-installation/packages/helm-v3.16.0-linux-amd64.tar.gz -C apps/kong/airgapped-installation/packages

sudo mv apps/kong/airgapped-installation/packages/linux-amd64/helm /usr/local/bin/helm
helm version

Install KONG:

helm template kong airgapped-installation/kong-2.37.0.tgz \
  --namespace kong \
  --values airgapped-installation/kong-values.yaml > base/kong-rendered.yaml



kubectl apply -n kong -k on-perm

You should get output like this:
    # Warning: 'bases' is deprecated. Please use 'resources' instead. Run 'kustomize edit fix' to update your Kustomization automatically.
    serviceaccount/kong-kong created
    role.rbac.authorization.k8s.io/kong-kong created
    clusterrole.rbac.authorization.k8s.io/kong-kong created
    rolebinding.rbac.authorization.k8s.io/kong-kong created
    clusterrolebinding.rbac.authorization.k8s.io/kong-kong created
    secret/kong-kong-validation-webhook-ca-keypair created
    secret/kong-kong-validation-webhook-keypair created
    secret/kong-enterprise-license created
    service/kong-kong-admin created
    service/kong-kong-manager created
    service/kong-kong-portal created
    service/kong-kong-portalapi created
    service/kong-kong-proxy created
    service/kong-kong-validation-webhook created
    deployment.apps/kong-kong created
    job.batch/kong-kong-init-migrations created
    job.batch/kong-kong-post-upgrade-migrations created
    job.batch/kong-kong-pre-upgrade-migrations created
    validatingwebhookconfiguration.admissionregistration.k8s.io/kong-kong-validations created

curl http://192.168.1.137:32081/status



Deploy the API GW Shush Auth Images
1.Copy all the images from apps/kong/airgapped-installation/image-bundles/shush-kong/per-image/ to the agnet nodes k3s/image folder 
2. restart the k3s agent & check the status to make sure its up and running
3. Deploy Cookie-generator-service:
    1. services/cookie-generator-service/k8s/on-perm/patch-env.yaml
            update BACKEND_API_URL to sherlock cluster endpoint
    2.  services/cookie-generator-service/k8s/base/deployment.yaml 
            update the image 
ISSSUE:

if any issues on :clear-stale-pid

        kubectl -n kong patch deploy kong-kong --type json -p='[
        { "op":"replace", "path":"/spec/template/spec/initContainers/0/command",
            "value":["/bin/sh","-lc","echo skip clear-stale-pid; true"] }
        ]'

        kubectl -n kong patch deploy kong-kong --type merge -p '{
        "spec":{"template":{"spec":{"securityContext":{"runAsUser":0,"fsGroup":0}}}}
        }'

        kubectl -n kong set image deploy/kong-kong \
        ingress-controller=docker.io/kong/kubernetes-ingress-controller:3.2

if its not working:
 kubectl -n kong patch deploy kong-kong --type json -p='[
  { "op":"remove", "path":"/spec/template/spec/initContainers" }]'

   kubectl -n kong rollout restart deploy/kong-kong