# AKS test run

This is the runbook used for the recorded AKS test run. It creates a small
single-node cluster, deploys the app, and tears the whole resource group down
again. It is intentionally not a production deployment.

## What Terraform creates

- `rg-football-oracle-aks` — resource group
- `acrfootballoracle` (or your chosen name) — Basic ACR
- `aks-football-oracle` — one `Standard_B2as_v2` node (2 vCPU / 8 GiB),
  `sku_tier = "Free"`, no autoscaler
- An `AcrPull` role assignment for the node's kubelet identity

The Azure for Students subscription in this project had 6 regional vCPUs and
a 4 vCPU B-family limit, so a default three-node cluster would fail. One
2 vCPU node fits. `swedencentral` was used because the subscription did not
list B-series SKUs in `germanywestcentral`.

## Prerequisites

- `az login` against the target subscription
- Terraform, Docker, `kubectl`
- A `.env` file based on `.env.example`, including a stable `PG_PASSWORD`
- Your public IP (the script reads it from `api.ipify.org`)

## 1. Provision the cluster

```powershell
$envMap = @{}
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*([A-Za-z0-9_]+)\s*=\s*(.*?)\s*$') {
    $envMap[$matches[1]] = $matches[2].Trim('"').Trim("'")
  }
}
foreach ($key in @('LITELLM_MASTER', 'APP_API_KEY', 'OPENCODE_API_KEY', 'PG_PASSWORD')) {
  if (-not $envMap[$key]) { throw ".env is missing $key" }
}

$ip = (Invoke-RestMethod https://api.ipify.org?format=json).ip
$acrName = "acrfootball$((Get-Random -Maximum 99999))"

terraform -chdir=terraform/aks init
terraform -chdir=terraform/aks apply `
  -var "subscription_id=$(az account show --query id -o tsv)" `
  -var "acr_name=$acrName" `
  -var "allowed_ip=$ip/32"
```

`acr_name` must be globally unique. If the generated name is taken, rerun with
another one. `allowed_ip` is required and locks both the AKS API server and the
app LoadBalancer to that CIDR.

## 2. Point kubectl at the cluster

```powershell
az aks get-credentials -g rg-football-oracle-aks -n aks-football-oracle `
  --context football-oracle-aks --overwrite-existing
```

## 3. Build and push the image

ACR Tasks are blocked on student subscriptions (`TasksOperationsNotAllowed`),
so build locally and push to the registry.

```powershell
$acr = terraform -chdir=terraform/aks output -raw acr_login_server
$acrName = terraform -chdir=terraform/aks output -raw acr_name
$tag = Get-Date -Format "yyyyMMddHHmm"

az acr login -n $acrName
docker build --build-arg "VITE_API_KEY=$($envMap['APP_API_KEY'])" `
  -f backend/Dockerfile -t "$acr/football-oracle-backend:$tag" .
docker push "$acr/football-oracle-backend:$tag"
```

If Docker Desktop's credential helper fails with `Not enough memory resources`,
restart Docker Desktop and retry the login. A Linux/WSL Docker context or a
machine with a working credential helper also avoids the Windows helper.

## 4. Create the namespace, ConfigMap, and Secret

```powershell
kubectl --context football-oracle-aks apply -f k8s/namespace.yaml

kubectl --context football-oracle-aks -n football-oracle create configmap litellm-config `
  --from-file=config.yaml=litellm.yaml --dry-run=client -o yaml |
  kubectl --context football-oracle-aks apply -f -

kubectl --context football-oracle-aks -n football-oracle create secret generic football-secrets `
  "--from-literal=app-key=$($envMap['LITELLM_MASTER'])" `
  "--from-literal=api-key=$($envMap['APP_API_KEY'])" `
  "--from-literal=pg-password=$($envMap['PG_PASSWORD'])" `
  "--from-literal=litellm-master=$($envMap['LITELLM_MASTER'])" `
  "--from-literal=opencode-key=$($envMap['OPENCODE_API_KEY'])" `
  "--from-literal=session-id=$($envMap['OPENCODE_SESSION_ID'])" `
  --dry-run=client -o yaml |
  kubectl --context football-oracle-aks apply -f -
```

The backend starts with the LiteLLM master key in `app-key`. To use a scoped
virtual key instead, mint one at `/key/generate` after LiteLLM is running and
patch the `app-key` value.

## 5. Deploy the workloads

```powershell
kubectl --context football-oracle-aks apply -f k8s/
kubectl --context football-oracle-aks -n football-oracle set image deployment/app `
  "backend=$acr/football-oracle-backend:$tag"
```

The manifest image is a placeholder; `set image` points it at the tag you just
pushed. The Postgres manifest already sets `PGDATA` to a subdirectory because
Azure Disk mounts contain `lost+found`.

## 6. Restrict the app LoadBalancer

`k8s/backend.yaml` ships with a documentation placeholder (`203.0.113.10/32`).
Patch it to your real IP:

```powershell
@"
spec:
  loadBalancerSourceRanges:
    - $ip/32
"@ | Set-Content -Encoding ascii svc-patch.yaml

kubectl --context football-oracle-aks -n football-oracle patch svc app `
  --type merge --patch-file svc-patch.yaml
Remove-Item svc-patch.yaml
```

## 7. Wait and open it

```powershell
kubectl --context football-oracle-aks -n football-oracle rollout status deployment/db --timeout=300s
kubectl --context football-oracle-aks -n football-oracle rollout status deployment/litellm --timeout=600s
kubectl --context football-oracle-aks -n football-oracle rollout status deployment/app --timeout=300s
kubectl --context football-oracle-aks -n football-oracle get svc app -w
```

Open `http://<EXTERNAL-IP>:8000` from the allowed IP.

## Teardown

Destroy the resource group, AKS node group, ACR, disks, and public IPs:

```powershell
$stateIp = terraform -chdir=terraform/aks output -raw allowed_ip
$stateAcr = terraform -chdir=terraform/aks output -raw acr_name

terraform -chdir=terraform/aks destroy `
  -var "subscription_id=$(az account show --query id -o tsv)" `
  -var "allowed_ip=$stateIp" `
  -var "acr_name=$stateAcr"

kubectl config delete-context football-oracle-aks
Remove-Item terraform/aks/terraform.tfstate,terraform/aks/terraform.tfstate.backup -ErrorAction SilentlyContinue
```

The Terraform state contains the AKS kubeconfig, so delete it after a successful
destroy. Check Cost Management the next day for any leftover disks or IPs.

## Troubleshooting

- **`TasksOperationsNotAllowed`** — ACR Tasks are disabled on the subscription.
  Build locally as above.
- **`Standard_B2as_v2` capacity or quota error** — try `Standard_B2als_v2`,
  `Standard_B2s_v2`, or `Standard_B2s` via
  `-var "node_vm_size=Standard_B2als_v2"`.
- **Postgres `initdb: directory exists but is not empty`** — the `PGDATA`
  subdirectory in `k8s/postgres.yaml` is the fix; keep it.
- **IP changed** — rerun `terraform apply` with the new `allowed_ip` and patch
  the Service source range.
- **LiteLLM is not ready yet** — it starts its Postgres migrations on first
  boot; give it a few minutes. The backend also retries a failed model once.
