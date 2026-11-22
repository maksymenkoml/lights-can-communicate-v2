"""Gateway and container sections that make a service configuration valid for the NAS profile."""

import copy

GATEWAY = {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A"}
CONTAINER = {
    "lan": {"parent": "ovs_eth0", "subnet": "192.168.99.0/24",
            "router": "192.168.99.1", "adapter_ip": "192.168.99.11"},
    "studio_hosts": ["nas.local"],
}


def sections(core_subnet=None, **gateway):
    """Fresh ``gateway`` and ``container`` sections; ``gateway`` overrides its fields."""
    container = copy.deepcopy(CONTAINER)
    if core_subnet is not None:
        container["core_subnet"] = core_subnet
    return {"gateway": {**GATEWAY, **gateway}, "container": container}
