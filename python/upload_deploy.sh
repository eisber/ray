#!/bin/sh
# az storage blob upload --account-name marcozopip -f dist/ray-0.9.0.dev0-cp37-cp37m-linux_x86_64.whl -c wheel -n ray-0.9.0.dev0-cp37-cp37m-linux_x86_64.whl
yaml=/home/marcozo/ray/python/ray/autoscaler/azure/example-full-marcozo.yaml

az storage blob upload --account-name marcozopip -f /home/marcozo/ray/.whl/ray-0.9.0.dev0-cp37-cp37m-manylinux1_x86_64.whl -c wheel -n ray-0.9.0.dev0-cp37-cp37m-linux_x86_64.whl
ray up -y $yaml

rg=`pcregrep -o1 "resource_group: (.*)" $yaml`
jumpbox_rg=mc-ray-jumpbox
peering_name=ray-to-jumpbox

echo peering vnets
az network vnet peering delete --resource-group $jumpbox_rg --vnet-name mc-ray-jumpbox-vnet -n $peering_name || true
az network vnet peering create -g $jumpbox_rg -n $peering_name --vnet-name mc-ray-jumpbox-vnet --remote-vnet /subscriptions/6187b663-b744-4d24-8226-7e66525baf8f/resourceGroups/$rg/providers/Microsoft.Network/virtualNetworks/ray-vnet --allow-vnet-access
az network vnet peering create -g $rg -n $peering_name --vnet-name ray-vnet  --remote-vnet /subscriptions/6187b663-b744-4d24-8226-7e66525baf8f/resourceGroups/$jumpbox_rg/providers/Microsoft.Network/virtualNetworks/mc-ray-jumpbox-vnet --allow-vnet-access

ssh-keygen -f "/home/marcozo/.ssh/known_hosts" -R "10.0.0.4"

echo ssh -L 8265:localhost:8265 -L 8899:localhost:8899 -o IdentitiesOnly=yes -i /home/marcozo/.ssh/ray_azure_${rg}_ubuntu.pem ubuntu@10.0.0.4
