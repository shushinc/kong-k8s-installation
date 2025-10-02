# K3s Air-Gapped Installation Guide

This document provides step-by-step instructions for installing **K3s v1.33.3+k3s1** in an **air-gapped environment** (no direct internet access).

---

## 1. Download Required Files (on internet machine)

```bash
# Download K3s binary
curl -Lo k3s https://github.com/k3s-io/k3s/releases/download/v1.33.3%2Bk3s1/k3s
chmod +x k3s

# Download K3s installer script
curl -Lo install.sh https://get.k3s.io
chmod +x install.sh

# Download SELinux RPM (for RHEL8/CentOS Stream/Rocky)
curl -LO https://github.com/k3s-io/k3s-selinux/releases/download/v1.6.stable.1/k3s-selinux-1.6-1.el8.noarch.rpm

# Download airgap container images
curl -LO https://github.com/k3s-io/k3s/releases/download/v1.33.3%2Bk3s1/k3s-airgap-images-amd64.tar.zst
```

---

## 2. Transfer Files to Air-Gapped Machines

Copy these files via `scp`, `rsync`, or USB:

- `k3s`
- `install.sh`
- `k3s-selinux-1.6-1.el8.noarch.rpm`
- `k3s-airgap-images-amd64.tar.zst`


sudo cp k3s-airgap-images-amd64.tar.zst /var/lib/rancher/k3s/agent/images/
sudo cp k3s /usr/local/bin/
sudo chmod +x /usr/local/bin/k3s
---


## 3. Install on Master Node

```bash
# Install SELinux policy
sudo yum install -y ./k3s-selinux-1.6-1.el8.noarch.rpm

# Place K3s binary
sudo cp k3s /usr/local/bin/
sudo chmod +x /usr/local/bin/k3s

# Load container images
sudo mkdir -p /var/lib/rancher/k3s/agent/images/
sudo cp k3s-airgap-images-amd64.tar.zst /var/lib/rancher/k3s/agent/images/

# Create config, make sure to update the MASTER_IP,NODE_TOKEN_FROM_MASTER,NODE_IP On each Node Respectively
sudo mkdir -p /etc/rancher/k3s
cat >/etc/rancher/k3s/config.yaml <<'YAML'
node-ip: <MASTER_IP>
tls-san:
  - <MASTER_IP>
kube-apiserver-arg:
  - advertise-address=<MASTER_IP>
flannel-iface: eth1
disable:
  - traefik
YAML

# Run installer
export INSTALL_K3S_SKIP_DOWNLOAD=true
export K3S_KUBECONFIG_MODE=644
export INSTALL_K3S_EXEC="server --disable traefik"

sudo ./install.sh
```

### Verify Master
```bash
systemctl status k3s --no-pager -l
ss -lntp | grep 6443 || true
    Expected Output:
        LISTEN 0      4096               :6443             *:    users:(("k3s-server",pid=7956,fd=12))
sudo kubectl get nodes (or) /usr/local/bin/kubectl get nodes
    Expected Output:
        NAME               STATUS   ROLES                  AGE    VERSION
        shush-sherlock-6   Ready    control-plane,master   5m7s   v1.33.4+k3s1

/usr/local/bin/kubectl get endpoints kubernetes -o wide
     Expected Output:

```


Copy the cluster token for agents:
```bash
cat /var/lib/rancher/k3s/server/node-token
    Expected Output :
        K1028846908454c410160333f9af89be9a0f4a1d65290a06ecff984aaec0b5d0deb::server:aa012e27dbc67942ed22cf4d8a824c00
```

---

## 4. Configure Agent Nodes 

On each agent:

```bash
# Place binary
sudo cp k3s /usr/local/bin/
sudo chmod +x /usr/local/bin/k3s

# Load images
sudo mkdir -p /var/lib/rancher/k3s/agent/images/
sudo cp k3s-airgap-images-amd64.tar.zst /var/lib/rancher/k3s/agent/images/

# List active network interfaces
ip -o -4 addr show | awk '{print $2, $4}'


# Create config, make sure to update the MASTER_IP,NODE_TOKEN_FROM_MASTER,NODE_IP On each Node Respectively
sudo mkdir -p /etc/rancher/k3s
cat >/etc/rancher/k3s/config.yaml <<'YAML'
server: https://<MASTER_IP>:6443
token: <NODE_TOKEN_FROM_MASTER>
node-ip: <NODE_IP>
flannel-iface: eth1 
YAML

# Run installer
export INSTALL_K3S_SKIP_DOWNLOAD=true
export INSTALL_K3S_EXEC="agent"
sudo ./install.sh
(or)
sudo -E INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_EXEC="agent" ./install.sh
```

### Verify Agent
```bash
systemctl status k3s-agent --no-pager -l
```


### Verify on Master Node
```bash
sudo kubectl get nodes -o wide (or) /usr/local/bin/kubectl get nodes
```

## 4. Notes

- The `k3s-airgap-images-*.tar.zst` includes all required system images.
- Custom application images must be imported manually as shown above.
- Ensure `node-ip` and `flannel-iface` are set correctly in `/etc/rancher/k3s/config.yaml`.

---
