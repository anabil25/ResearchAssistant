targetScope = 'resourceGroup'

param name string
param location string
param tags object = {}
param storageAccountId string
param cosmosAccountId string

var containerAppsSubnetName = 'snet-container-apps'
var privateEndpointSubnetName = 'snet-private-endpoints'
var functionsSubnetName = 'snet-connector-functions'
var blobPrivateDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var queuePrivateDnsZoneName = 'privatelink.queue.${environment().suffixes.storage}'
var tablePrivateDnsZoneName = 'privatelink.table.${environment().suffixes.storage}'
// Azure clouds currently expose no Cosmos DB suffix through environment().
#disable-next-line no-hardcoded-env-urls
var cosmosPrivateDnsZoneName = 'privatelink.documents.azure.com'

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-app-${name}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        '10.70.0.0/24'
      ]
    }
    subnets: [
      {
        name: containerAppsSubnetName
        properties: {
          addressPrefix: '10.70.0.0/27'
          delegations: [
            {
              name: 'Microsoft.App/environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: privateEndpointSubnetName
        properties: {
          addressPrefix: '10.70.0.32/28'
          privateEndpointNetworkPolicies: 'Disabled'
        }
      }
      {
        name: functionsSubnetName
        properties: {
          addressPrefix: '10.70.0.64/27'
          delegations: [
            {
              name: 'Microsoft.App/environments'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
    ]
  }
}

resource containerAppsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: containerAppsSubnetName
}

resource privateEndpointSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}

resource functionsSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: functionsSubnetName
}

resource blobPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: blobPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource blobDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: blobPrivateDns
  name: 'link-${name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource storageBlobPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-blob-${name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'blob'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'blob'
          ]
        }
      }
    ]
  }
}

resource blobDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageBlobPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'blob'
        properties: {
          privateDnsZoneId: blobPrivateDns.id
        }
      }
    ]
  }
  dependsOn: [
    blobDnsLink
  ]
}

resource queuePrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: queuePrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource queueDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: queuePrivateDns
  name: 'link-${name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource storageQueuePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-queue-${name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'queue'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'queue'
          ]
        }
      }
    ]
  }
}

resource queueDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageQueuePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'queue'
        properties: {
          privateDnsZoneId: queuePrivateDns.id
        }
      }
    ]
  }
  dependsOn: [
    queueDnsLink
  ]
}

resource tablePrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: tablePrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource tableDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: tablePrivateDns
  name: 'link-${name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource storageTablePrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-table-${name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'table'
        properties: {
          privateLinkServiceId: storageAccountId
          groupIds: [
            'table'
          ]
        }
      }
    ]
  }
}

resource tableDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: storageTablePrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'table'
        properties: {
          privateDnsZoneId: tablePrivateDns.id
        }
      }
    ]
  }
  dependsOn: [
    tableDnsLink
  ]
}

resource cosmosPrivateDns 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: cosmosPrivateDnsZoneName
  location: 'global'
  tags: tags
}

resource cosmosDnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: cosmosPrivateDns
  name: 'link-${name}'
  location: 'global'
  tags: tags
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: vnet.id
    }
  }
}

resource cosmosSqlPrivateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pe-cosmos-${name}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnet.id
    }
    privateLinkServiceConnections: [
      {
        name: 'sql'
        properties: {
          privateLinkServiceId: cosmosAccountId
          groupIds: [
            'Sql'
          ]
        }
      }
    ]
  }
}

resource cosmosDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: cosmosSqlPrivateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'sql'
        properties: {
          privateDnsZoneId: cosmosPrivateDns.id
        }
      }
    ]
  }
  dependsOn: [
    cosmosDnsLink
  ]
}

output containerAppsSubnetId string = containerAppsSubnet.id
output functionsSubnetId string = functionsSubnet.id
output vnetId string = vnet.id
output blobPrivateEndpointId string = storageBlobPrivateEndpoint.id
output queuePrivateEndpointId string = storageQueuePrivateEndpoint.id
output tablePrivateEndpointId string = storageTablePrivateEndpoint.id
output cosmosPrivateEndpointId string = cosmosSqlPrivateEndpoint.id
