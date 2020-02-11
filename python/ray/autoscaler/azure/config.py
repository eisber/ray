import datetime
import json
import logging
import os
import secrets
import string
import time
import uuid

from azure.common.exceptions import CloudError, AuthenticationError
from azure.common.client_factory import get_client_from_cli_profile
from azure.common.client_factory import get_client_from_auth_file
from azure.graphrbac import GraphRbacManagementClient
from azure.mgmt.authorization import AuthorizationManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient
import paramiko

# TODO: add asserts on validity of names for all resources
RAY = "ray-autoscaler"
PASSWORD_MIN_LENGTH = 16
SSH_KEYS_MAX_COUNT = 10
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
        "linux_configuration": {
            "disable_password_authentication": True,
        }
    },
    "priority": "Spot",
    "evictionPolicy": "Deallocate",
    "billingProfile": {
        "maxPrice": -1
    }
}

logger = logging.getLogger(__name__)


def bootstrap_azure(config):
    config = _configure_resource_group(config)
    config = _configure_service_principal(config)
    config = _configure_key_pair(config)
    config = _configure_network(config)
    config = _configure_nodes(config)
    return config


def _configure_resource_group(config):
    # TODO: look at availability sets
    # https://docs.microsoft.com/en-us/azure/virtual-machines/windows/tutorial-availability-sets
    kwargs = dict()
    if "subscription_id" in config["provider"]:
        kwargs["subscription_id"] = config["provider"]["subscription_id"]
    resource_client = get_client_from_cli_profile(
        client_class=ResourceManagementClient, **kwargs)
    logger.info("Using subscription id: %s",
                resource_client.config.subscription_id)
    config["provider"][
        "subscription_id"] = resource_client.config.subscription_id

    resource_group_name = config["provider"]["resource_group"]
    logger.info("Creating resource group: %s", resource_group_name)
    params = dict(location=config["provider"]["location"])
    resource_client.resource_groups.create_or_update(
        resource_group_name=resource_group_name, parameters=params)

    return config


# Modeled after create_service_principal_for_rbac in
#  https://github.com/Azure/azure-cli/blob/dev/src/azure-cli/azure/cli/command_modules/role/custom.py
def _configure_service_principal(config):
    graph_client = get_client_from_cli_profile(GraphRbacManagementClient)
    resource_client = get_client_from_cli_profile(ResourceManagementClient)
    auth_client = get_client_from_cli_profile(AuthorizationManagementClient)

    sp_name = config["provider"]["service_principal"]
    if "://" not in sp_name:
        app_name = sp_name
        sp_name = "http://" + sp_name
    else:
        app_name = sp_name.split("://", 1)[-1]

    resource_group = config["provider"]["resource_group"]
    auth_name = "azure_credentials_{}.json".format(app_name)
    auth_path = os.path.expanduser("~/.azure/{}".format(auth_name))

    new_auth = False
    if os.path.exists(auth_path):
        with open(auth_path, "r") as f:
            credentials = json.load(f)
        password = credentials["clientSecret"]
    else:
        new_auth = True
        logger.info("Generating new password for auth file")
        # TODO: seems like uuid4 is possible? revisit simplifying password
        alphabet = "".join([
            string.ascii_lowercase, string.ascii_uppercase, string.digits,
            string.punctuation
        ])
        while True:
            password = "".join(
                secrets.choice(alphabet) for _ in range(PASSWORD_MIN_LENGTH))
            if (any(c.islower() for c in password) and any(c.isupper()
                                                           for c in password)
                    and any(c.isdigit() for c in password)
                    and any(not c.isalnum() for c in password)):
                break

    try:
        # find existing application
        app = graph_client.applications.list(
            filter="displayName eq \"{}\"".format(app_name)).next()
        logger.info("Found Application: %s", app_name)
    except StopIteration:
        # create new application
        new_auth = True
        logger.info("Creating Application: %s", app_name)
        app_start_date = datetime.datetime.now(datetime.timezone.utc)
        app_end_date = app_start_date.replace(year=app_start_date.year + 1)

        password_credentials = dict(
            start_date=app_start_date,
            end_date=app_end_date,
            key_id=uuid.uuid4().hex,
            value=password)
        app_params = dict(
            display_name=app_name,
            identifier_uris=[sp_name],
            password_credentials=[password_credentials])
        app = graph_client.applications.create(parameters=app_params)

    try:
        query_exp = "servicePrincipalNames/any(x:x eq \"{}\")".format(sp_name)
        sp = graph_client.service_principals.list(filter=query_exp).next()
        logger.info("Found Service Principal: %s", sp_name)
    except StopIteration:
        # create new service principal
        logger.info("Creating Service Principal: %s", sp_name)
        sp_params = dict(app_id=app.app_id)
        sp = graph_client.service_principals.create(parameters=sp_params)

    # TODO: check if sp already has correct role / scope
    for _ in range(RETRIES):
        try:
            # set contributor role for service principal on new resource group
            rg_id = resource_client.resource_groups.get(resource_group).id
            role = auth_client.role_definitions.list(
                rg_id, filter="roleName eq \"Contributor\"").next()
            role_params = dict(
                role_definition_id=role.id, principal_id=sp.object_id)
            auth_client.role_assignments.create(
                scope=rg_id,
                role_assignment_name=uuid.uuid4(),
                parameters=role_params)
            break
        except CloudError:
            time.sleep(1)

    if new_auth:
        credentials = dict(
            clientSecret=password,
            clientId=app.app_id,
            subscriptionId=config["provider"]["subscription_id"],
            tenantId=graph_client.config.tenant_id)
        credentials.update(AUTH_ENDPOINTS)
        with open(auth_path, "w") as f:
            json.dump(credentials, f)

    config["provider"]["auth_path"] = auth_path
    return config


def _configure_key_pair(config):
    # skip key generation if it is manually specified
    ssh_private_key = config["auth"].get("ssh_private_key")
    if ssh_private_key:
        assert os.path.exists(ssh_private_key)
        return config

    location = config["provider"]["location"]
    resource_group = config["provider"]["resource_group"]
    ssh_user = config["auth"]["ssh_user"]

    # look for an existing key pair
    # TODO: other services store key pairs in cloud? why try multiple times?
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
    config["provider"]["ssh_public_key_data"] = public_key

    return config


def _configure_network(config):
    # skip this if nic is manually set in configuration yaml
    head_node_nic = config["head_node"].get("network_profile", {}).get(
        "network_interfaces", [])
    worker_nodes_nic = config["worker_nodes"].get("network_profile", {}).get(
        "network_interfaces", [])
    if head_node_nic and worker_nodes_nic:
        return config

    location = config["provider"]["location"]
    resource_group = config["provider"]["resource_group"]
    auth_path = config["provider"]["auth_path"]
    network_client = get_client_from_auth_file(
        NetworkManagementClient, auth_path=auth_path)

    vnets = []
    for _ in range(RETRIES):
        try:
            vnets = list(
                network_client.virtual_networks.list(
                    resource_group_name=resource_group,
                    filter="name eq \"{}\"".format(VNET_NAME)))
            break
        except AuthenticationError:
            # wait for service principal authorization to populate
            time.sleep(1)

    # can"t update vnet if subnet already exists
    if not vnets:
        # create VNet
        logger.info("Creating VNet: %s", VNET_NAME)
        vnet_params = dict(
            location=location,
            address_space=dict(address_prefixes=["10.0.0.0/16"]))
        network_client.virtual_networks.create_or_update(
            resource_group_name=resource_group,
            virtual_network_name=VNET_NAME,
            parameters=vnet_params).wait()

    # create Subnet
    logger.info("Creating Subnet: %s", SUBNET_NAME)
    subnet_params = dict(address_prefix="10.0.0.0/24")
    subnet = network_client.subnets.create_or_update(
        resource_group_name=resource_group,
        virtual_network_name=VNET_NAME,
        subnet_name=SUBNET_NAME,
        subnet_parameters=subnet_params).result()

    config["provider"]["subnet_id"] = subnet.id

    # create NSG
    logger.info("Creating NSG: %s", NSG_NAME)
    nsg_params = {
        "name": NSG_NAME,
        "location": config["location"],
        "security_rules": [{
            "name": "ssh",
            "access": "Allow",
            "priority": 300,
            "destination_port_range": "22"
        }]
    }
    network_client.network_security_groups.create_or_update(
        resource_group_name=resource_group,
        network_security_group_name=NSG_NAME,
        parameters=nsg_params)

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
    if "head_node" not in config:
        config["head_node"] = DEFAULT_NODE_CONFIG
    if "worker_nodes" not in config:
        config["worker_nodes"] = DEFAULT_NODE_CONFIG
    return config
