# Only instantiated when network_isolation is on. Everything here exists to make
# the PaaS resources unreachable from the internet: private endpoints give them
# addresses inside this VNet, and the Private DNS zones make their public names
# resolve to those addresses instead.

resource "azurerm_virtual_network" "this" {
  name                = "vnet-${var.name_prefix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  address_space       = [var.address_space]
  tags                = var.tags
}

resource "azurerm_subnet" "compute" {
  name                 = "snet-compute"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.address_space, 8, 1)]
}

resource "azurerm_subnet" "private_endpoints" {
  name                 = "snet-private-endpoints"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.address_space, 8, 2)]
}

resource "azurerm_subnet" "scoring" {
  name                 = "snet-scoring"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.address_space, 8, 3)]
}

# The Container Apps environment that hosts the page.
#
# /23 rather than the /27 minimum, and the index jumps to a /23-aligned block
# well clear of the three /24s above rather than continuing the sequence.
#
# Container Apps consumes addresses per replica *and* per revision, and it holds
# the ones belonging to a superseded revision until that revision is fully
# deprovisioned -- so a subnet sized to steady-state demand runs out during the
# one operation that matters, a rollout. The subnet also cannot be resized after
# the environment is created: growing it means recreating the environment, which
# changes the FQDN. /23 is 512 addresses for a page that will use a handful, and
# the address space is private and free, so the only thing a smaller prefix would
# buy is the possibility of that migration.
resource "azurerm_subnet" "apps" {
  name                 = "snet-apps"
  resource_group_name  = var.resource_group_name
  virtual_network_name = azurerm_virtual_network.this.name
  address_prefixes     = [cidrsubnet(var.address_space, 7, 4)]

  # Required for a workload-profiles environment, and forbidden for the legacy
  # Consumption-only one. This deployment uses workload profiles, so the
  # delegation is mandatory: without it the environment fails to create.
  delegation {
    name = "container-apps"
    service_delegation {
      name    = "Microsoft.App/environments"
      actions = ["Microsoft.Network/virtualNetworks/subnets/action"]
    }
  }
}

# --- NSGs ---------------------------------------------------------------------
# Azure's default rules already allow intra-VNet traffic and deny inbound from
# the internet. These make the deny explicit and higher priority than anything a
# later change might add by accident.

resource "azurerm_network_security_group" "compute" {
  name                = "nsg-${var.name_prefix}-compute"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "deny-internet-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }

  # Azure ML's control plane reaches compute nodes on these ports. Without this
  # the cluster provisions and then reports itself unusable.
  security_rule {
    name                       = "allow-azureml-inbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["29876", "29877", "44224"]
    source_address_prefix      = "AzureMachineLearning"
    destination_address_prefix = "*"
  }
}

resource "azurerm_network_security_group" "private_endpoints" {
  name                = "nsg-${var.name_prefix}-pe"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "deny-internet-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

# snet-scoring holds the managed online endpoint's outbound integration. It had
# no NSG at all while its two sibling subnets had one each -- an omission rather
# than a decision, and the kind that is invisible because the subnet still works
# perfectly without it.
resource "azurerm_network_security_group" "scoring" {
  name                = "nsg-${var.name_prefix}-scoring"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "deny-internet-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "compute" {
  subnet_id                 = azurerm_subnet.compute.id
  network_security_group_id = azurerm_network_security_group.compute.id
}

resource "azurerm_subnet_network_security_group_association" "private_endpoints" {
  subnet_id                 = azurerm_subnet.private_endpoints.id
  network_security_group_id = azurerm_network_security_group.private_endpoints.id
}

resource "azurerm_subnet_network_security_group_association" "scoring" {
  subnet_id                 = azurerm_subnet.scoring.id
  network_security_group_id = azurerm_network_security_group.scoring.id
}

# snet-apps is the one subnet here that is *supposed* to receive traffic from the
# internet: the page is deliberately public (see infra/azure/README.md). So it
# gets explicit allows for the ingress path rather than the blanket
# deny-internet-inbound its siblings carry, and the deny sits underneath them.
#
# Outbound is left at the platform default, and that is a decision rather than an
# oversight. Container Apps needs egress to Entra ID, the registry, the Container
# Apps control plane and Azure Monitor merely to start a replica, and a
# destination allow-list that is wrong fails as a container that never becomes
# ready -- with no NSG flow log to say why, because dev does not run one. The
# compensating control is that the identity reachable from this subnet holds
# three read roles and AcrPull and can therefore exfiltrate nothing it could not
# already display.
resource "azurerm_network_security_group" "apps" {
  # checkov:skip=CKV_AZURE_160:port 80 is open so the ingress can answer it with its own 301. allow_insecure_connections = false means the container app never serves content over plaintext -- the listener exists only to redirect. Closing it here would not remove a plaintext path, it would replace a redirect with a timeout for anyone who typed http://, which is worse for the user and no better for the attacker. The claim is checked rather than asserted: smoke_test_app.sh section [3] fails the deploy if http:// ever returns 200 instead of a redirect or a refusal.
  name                = "nsg-${var.name_prefix}-apps"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags

  security_rule {
    name                       = "allow-https-inbound"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_ranges    = ["80", "443"]
    source_address_prefix      = "Internet"
    destination_address_prefix = "VirtualNetwork"
  }

  # The environment's own load balancer health checks. Without this the platform
  # cannot probe replicas and the revision never reports healthy.
  security_rule {
    name                       = "allow-load-balancer-inbound"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "AzureLoadBalancer"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "deny-other-inbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "Internet"
    destination_address_prefix = "*"
  }
}

resource "azurerm_subnet_network_security_group_association" "apps" {
  subnet_id                 = azurerm_subnet.apps.id
  network_security_group_id = azurerm_network_security_group.apps.id
}

# --- private endpoints and DNS ------------------------------------------------

resource "azurerm_private_dns_zone" "this" {
  for_each = {
    for key, target in var.private_endpoint_targets : target.dns_zone => target...
  }

  name                = each.key
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_private_dns_zone_virtual_network_link" "this" {
  for_each = azurerm_private_dns_zone.this

  name                  = "link-${replace(each.key, ".", "-")}"
  resource_group_name   = var.resource_group_name
  private_dns_zone_name = each.value.name
  virtual_network_id    = azurerm_virtual_network.this.id
  registration_enabled  = false
  tags                  = var.tags
}

resource "azurerm_private_endpoint" "this" {
  for_each = var.private_endpoint_targets

  name                = "pe-${var.name_prefix}-${each.key}"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = azurerm_subnet.private_endpoints.id
  tags                = var.tags

  private_service_connection {
    name                           = "psc-${each.key}"
    private_connection_resource_id = each.value.resource_id
    subresource_names              = [each.value.subresource]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "dns-${each.key}"
    private_dns_zone_ids = [azurerm_private_dns_zone.this[each.value.dns_zone].id]
  }
}
