import datetime
import json
import logging
import os
import pathlib
import secrets
import string
import time
import uuid

from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerinstance.models import (ContainerGroup,
                                                 ContainerGroupIdentity,
                                                 ContainerGroupIdentityUserAssignedIdentitiesValue,
                                                 Container,
                                                 ContainerGroupNetworkProtocol,
                                                 ContainerGroupRestartPolicy,
                                                 ContainerPort,
                                                 EnvironmentVariable,
                                                 IpAddress,
                                                 Port,
                                                 ResourceRequests,
                                                 GpuResource,
                                                 ResourceRequirements,
                                                 OperatingSystemTypes)
from azure.mgmt.compute.models import ResourceIdentityType

from azure.common.exceptions import CloudError, AuthenticationError
from azure.common.client_factory import get_client_from_cli_profile
from azure.graphrbac import GraphRbacManagementClient
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.msi import ManagedServiceIdentityClient
import paramiko
from ray.autoscaler.tags import TAG_RAY_NODE_NAME

RAY = "ray-autoscaler"
PASSWORD_MIN_LENGTH = 16
RETRIES = 10
SUBNET_NAME = "ray-subnet"
NSG_NAME = "ray-nsg"
VNET_NAME = "ray-vnet"
AUTH_ENDPOINTS = {
    "activeDirectoryEndpointUrl": "https://login.microsoftonline.com",
    "resourceManagerEndpointUrl": "https://management.azure.com/",
    "activeDirectoryGraphResourceId": "https://graph.windows.net/",
    "sqlManagementEndpointUrl": "https://management.core.windows.net:8443/",
    "galleryEndpointUrl": "https://gallery.azure.com/",
    "managementEndpointUrl": "https://management.core.windows.net/"
}
DEFAULT_NODE_CONFIG = {
    "hardware_profile": {
        "vm_size": "Standard_D2s_v3"
    },
    "storage_profile": {
        "os_disk": {
            "create_option": "FromImage",
            "caching": "ReadWrite"
        },
        "image_reference": {
            "publisher": "microsoft-dsvm",
            "offer": "linux-data-science-vm-ubuntu",
            "sku": "linuxdsvmubuntu",
            "version": "latest"
        }
    },
    "os_profile": {
        "admin_username": "ubuntu",
        "computer_name": TAG_RAY_NODE_NAME,
        "linux_configuration": {
            "disable_password_authentication": True,
            "ssh": {
                "public_keys": None  # populated by _configure_key_pair
            }
        }
    }
}

logger = logging.getLogger(__name__)

def bootstrap_aci(config):
    config = _configure_resource_group(config)
    config = _configure_msi_user(config)
    config = _configure_key_pair(config)
    config = _configure_container_group(config)
    config = _configure_nodes(config)
    return config

def _get_client_from_cli_profile_with_subscription_id(client_class, config):
    kwargs = {}
    if "subscription_id" in config["provider"]:
        kwargs["subscription_id"] = config["provider"]["subscription_id"]

    return get_client_from_cli_profile(client_class=client_class, **kwargs)


def _configure_msi_user(config):
    msi_client = _get_client_from_cli_profile_with_subscription_id(ManagedServiceIdentityClient, config)
    resource_client = _get_client_from_cli_profile_with_subscription_id(ResourceManagementClient, config)
    auth_client = _get_client_from_cli_profile_with_subscription_id(AuthorizationManagementClient, config)

    resource_group = config["provider"]["resource_group"]
    location = config["provider"]["location"]

    logger.info("Creating MSI user assigned identity")

    rg_id = resource_client.resource_groups.get(resource_group).id

    identities = list(msi_client.user_assigned_identities.list_by_resource_group(resource_group))
    if len(identities) > 0:
        user_assigned_identity = identities[0]
    else:
        user_assigned_identity = msi_client.user_assigned_identities.create_or_update(
            resource_group,
            str(uuid.uuid4()), # Any name, just a human readable ID
            location
        )

    config["provider"]["msi_identity_id"] = user_assigned_identity.id
    config["provider"]["msi_identity_principal_id"] = user_assigned_identity.principal_id

    def assign_role():
        for _ in range(RETRIES):
            try:
                role = auth_client.role_definitions.list(
                    rg_id, filter="roleName eq 'Contributor'").next()
                role_params = {
                    "role_definition_id": role.id,
                    "principal_id": user_assigned_identity.principal_id
                }

                for assignment in auth_client.role_assignments.list_for_scope(
                    rg_id, 
                    filter="principalId eq '{principal_id}'".format(**role_params)):

                    if (assignment.role_definition_id == role.id):
                        return

                auth_client.role_assignments.create(
                    scope=rg_id,
                    role_assignment_name=uuid.uuid4(),
                    parameters=role_params)
                logger.info("Creating contributor role assignment")
                return
            except CloudError as ce:
                if str(ce.error).startswith("Azure Error: PrincipalNotFound"):
                    time.sleep(3)
                else:
                    raise

        raise Exception("Failed to create contributor role assignment")

    assign_role()

    return config

def _configure_resource_group(config):
    # TODO: look at availability sets
    # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/tutorial-availability-sets
    resource_client = _get_client_from_cli_profile_with_subscription_id(ResourceManagementClient, config)

    subscription_id = resource_client.config.subscription_id
    logger.info("Using subscription id: %s", subscription_id)
    config["provider"]["subscription_id"] = subscription_id

    resource_group = config["provider"]["resource_group"]
    params = {"location": config["provider"]["location"]}

    if "tags" in config["provider"]:
        params["tags"] = config["provider"]["tags"]

    logger.info("Creating resource group: %s", resource_group)
    resource_client.resource_groups.create_or_update(
        resource_group_name=resource_group, parameters=params)

    return config

def _configure_container_group(config):
    aci_client = _get_client_from_cli_profile_with_subscription_id(ContainerInstanceManagementClient, config)

    container_group_name = config["docker"]["container_name"]
    container_image_name = config["docker"]["image"]
    location = config["provider"]["location"]
    resource_group = config["provider"]["resource_group"]

    # Configure the container
    container_resource_requests = ResourceRequests(
        memory_in_gb=2,
        cpu=1.0, 
        gpu=GpuResource(
            count=1,
            sku="k80"
        ))
    container_resource_requirements = ResourceRequirements(
        requests=container_resource_requests)
    container = Container(name=container_group_name,
                          image=container_image_name,
                          resources=container_resource_requirements,
                          ports=[ContainerPort(port=22)])
    
    # Configure the container group
    ports = [Port(protocol=ContainerGroupNetworkProtocol.tcp, port=22)]
    group_ip_address = IpAddress(ports=ports,
                                 dns_name_label=container_group_name,
                                 type="Public")

    user_assigned_identities = {}
    user_assigned_identities[config["provider"]["msi_identity_id"]] = ContainerGroupIdentityUserAssignedIdentitiesValue()

    group = ContainerGroup(location=location,
                           containers=[container],
                           os_type=OperatingSystemTypes.linux,
                           ip_address=group_ip_address,
                           identity=ContainerGroupIdentity(
                                type=ResourceIdentityType.user_assigned,
                                user_assigned_identities=user_assigned_identities
                           ))

    # Create the container group
    aci_client.container_groups.create_or_update(resource_group,
                                                 container_group_name,
                                                 group)

    # Get the created container group
    container_group = aci_client.container_groups.get(resource_group,
                                                      container_group_name)

    return config

def _configure_key_pair(config):
    # skip key generation if it is manually specified
    ssh_private_key = config["auth"].get("ssh_private_key")
    if ssh_private_key:
        assert os.path.exists(ssh_private_key)
        # make sure public key configuration also exists
        for node_type in ["head_node", "worker_nodes"]:
            os_profile = config[node_type]["os_profile"]
            assert os_profile["linux_configuration"]["ssh"]["public_keys"]
        return config

    location = config["provider"]["location"]
    resource_group = config["provider"]["resource_group"]
    ssh_user = config["auth"]["ssh_user"]

    # look for an existing key pair
    key_name = "{}_azure_{}_{}".format(RAY, location, resource_group, ssh_user)
    public_key_path = os.path.expanduser("~/.ssh/{}.pub".format(key_name))
    private_key_path = os.path.expanduser("~/.ssh/{}.pem".format(key_name))
    if os.path.exists(public_key_path) and os.path.exists(private_key_path):
        logger.info("SSH key pair found: %s", key_name)
        with open(public_key_path, "r") as f:
            public_key = f.read()
    else:
        public_key, private_key_path = _generate_ssh_keys(key_name)
        logger.info("SSH key pair created: %s", key_name)

    config["auth"]["ssh_private_key"] = private_key_path

    public_keys = [{
        "key_data": public_key,
        "path": "/home/{}/.ssh/authorized_keys".format(
            config["auth"]["ssh_user"])
    }]
    for node_type in ["head_node", "worker_nodes"]:
        os_config = DEFAULT_NODE_CONFIG["os_profile"].copy()
        os_config["linux_configuration"]["ssh"]["public_keys"] = public_keys
        config_type = config.get(node_type, {})
        config_type.update({"os_profile": os_config})
        config[node_type] = config_type

    return config


def _generate_ssh_keys(key_name):
    """Generate and store public and private keys"""
    public_key_path = os.path.expanduser("~/.ssh/{}.pub".format(key_name))
    private_key_path = os.path.expanduser("~/.ssh/{}.pem".format(key_name))

    ssh_dir, _ = os.path.split(private_key_path)
    if not os.path.exists(ssh_dir):
        os.makedirs(ssh_dir)
        os.chmod(ssh_dir, 0o700)

    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(private_key_path)
    os.chmod(private_key_path, 0o600)

    with open(public_key_path, "w") as public_key_file:
        # TODO: check if this is the necessary format
        public_key = "%s %s" % (key.get_name(), key.get_base64())
        public_key_file.write(public_key)
    os.chmod(public_key_path, 0o644)

    return public_key, private_key_path


def _configure_nodes(config):
    """Add default node configuration if not provided"""
    for node_type in ["head_node", "worker_nodes"]:
        node_config = DEFAULT_NODE_CONFIG.copy()
        node_config.update(config.get(node_type, {}))
        config[node_type] = node_config
    return config
