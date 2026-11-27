"""Gateway and container sections that make a service configuration valid (version 2)."""

import copy

GATEWAY = {"lan_ip": "192.168.99.10", "mac": "02:00:00:00:00:0A"}
CONTAINER = {"studio_hosts": ["nas.local"]}


def sections(core_address=None, **gateway):
    """Fresh ``gateway`` and ``container`` sections; ``gateway`` overrides its fields."""
    container = copy.deepcopy(CONTAINER)
    if core_address is not None:
        container["core_address"] = core_address
    return {"gateway": {**GATEWAY, **gateway}, "container": container}
