# Infrastructure only: RG + ACR + AKS. The app image, Secret, and k8s
# manifests are deployed with the short runbook in README.md, not Terraform.
terraform {
  required_version = ">= 1.5"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

locals {
  tags = {
    project = "football-oracle"
    purpose = "demo"
    ttl     = "1d"
  }
}

resource "azurerm_resource_group" "aks" {
  name     = var.resource_group_name
  location = var.location
  tags     = local.tags
}

# Basic registry: ~$0.17/day. Holds only the backend image for this demo.
resource "azurerm_container_registry" "acr" {
  name                = var.acr_name
  resource_group_name = azurerm_resource_group.aks.name
  location            = azurerm_resource_group.aks.location
  sku                 = "Basic"
  admin_enabled       = false
  tags                = local.tags
}

# One node on purpose. Student subscriptions have ~6 regional vCPUs; the
# default 3 x 2 vCPU pool is what makes az aks create fail. Free control
# plane, single B-series node, no autoscaling: 2 vCPUs total.
resource "azurerm_kubernetes_cluster" "aks" {
  name                = var.cluster_name
  location            = azurerm_resource_group.aks.location
  resource_group_name = azurerm_resource_group.aks.name
  dns_prefix          = var.dns_prefix
  kubernetes_version  = var.kubernetes_version
  sku_tier            = "Free"
  tags                = local.tags

  default_node_pool {
    name                 = "system"
    node_count           = 1
    vm_size              = var.node_vm_size
    os_sku               = "AzureLinux"
    os_disk_size_gb      = 30
    max_pods             = 30
    auto_scaling_enabled = false
    type                 = "VirtualMachineScaleSets"

    # Explicit defaults: AKS reports this block even when it is omitted, which
    # otherwise makes every plan show a spurious update.
    upgrade_settings {
      drain_timeout_in_minutes      = 0
      max_surge                     = "10%"
      node_soak_duration_in_minutes = 0
    }
  }

  identity {
    type = "SystemAssigned"
  }

  network_profile {
    network_plugin    = "kubenet"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
  }

  # The API server is public but reachable only from this CIDR. The deploy
  # script passes your current public IP.
  api_server_access_profile {
    authorized_ip_ranges = [var.allowed_ip]
  }
}

# Let the node's kubelet identity pull from the registry without an
# imagePullSecret. Requires Owner/User Access Administrator (student
# subscriptions grant Owner on the subscription).
resource "azurerm_role_assignment" "acr_pull" {
  scope                = azurerm_container_registry.acr.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_kubernetes_cluster.aks.kubelet_identity[0].object_id
}
