output "resource_group" {
  value = azurerm_resource_group.aks.name
}

output "location" {
  value = azurerm_resource_group.aks.location
}

output "cluster_name" {
  value = azurerm_kubernetes_cluster.aks.name
}

output "acr_name" {
  value = azurerm_container_registry.acr.name
}

output "acr_login_server" {
  value = azurerm_container_registry.acr.login_server
}

output "node_resource_group" {
  value = azurerm_kubernetes_cluster.aks.node_resource_group
}

output "allowed_ip" {
  value = var.allowed_ip
}

output "kubeconfig_command" {
  value = "az aks get-credentials -g ${azurerm_resource_group.aks.name} -n ${azurerm_kubernetes_cluster.aks.name} --context football-oracle-aks --overwrite-existing"
}
