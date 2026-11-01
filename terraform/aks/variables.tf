variable "subscription_id" {
  description = "Azure subscription id. Null uses the current az CLI subscription."
  type        = string
  default     = null
  nullable    = true
}

variable "location" {
  description = "Azure region. B-series capacity varies by region and subscription."
  type        = string
  default     = "swedencentral"
}

variable "resource_group_name" {
  description = "Resource group for the AKS demo."
  type        = string
  default     = "rg-football-oracle-aks"
}

variable "cluster_name" {
  description = "AKS cluster name."
  type        = string
  default     = "aks-football-oracle"
}

variable "dns_prefix" {
  description = "AKS DNS prefix (only used for the API server FQDN)."
  type        = string
  default     = "football-oracle-aks"
}

variable "node_vm_size" {
  description = "Node size. B2as_v2 = 2 vCPU / 8 GiB, inside the B-family and regional quota. B2s (2/4) is the fallback."
  type        = string
  default     = "Standard_B2as_v2"
}

variable "kubernetes_version" {
  description = "AKS version. Null lets AKS pick the current default."
  type        = string
  default     = null
  nullable    = true
}

variable "acr_name" {
  description = "Globally unique ACR name (alphanumeric only). Change it if the default is taken."
  type        = string
  default     = "acrfootballoracle"

  validation {
    condition     = can(regex("^[a-zA-Z0-9]{5,50}$", var.acr_name))
    error_message = "ACR names must be 5-50 alphanumeric characters."
  }
}

variable "allowed_ip" {
  description = "Your public IP in CIDR form. Used for the AKS API server and the app LoadBalancer."
  type        = string

  validation {
    condition     = can(cidrhost(var.allowed_ip, 0))
    error_message = "allowed_ip must be a CIDR such as 203.0.113.10/32."
  }
}
