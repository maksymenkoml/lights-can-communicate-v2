"""Release layout constants and the endpoints of the core's components."""

ARCHIVE = "lights-can-communicate-v2.pyz"
GATEWAY_MODULES = (
    "bootstrap",
    "bridge_output",
    "power_feedback",
    "hue_power_requests",
    "hue_events",
    "entertainment_stream",
    "entertainment_receiver",
    "udp_port_guard",
    "stream_http",
    "stream_diagnostics",
    "scene_play_intent",
    "scene_edits",
    "gradient_profile",
    "hue_resource_compat",
    "hue_names",
    "desk_layout",
    "rooms",
    "room_groups",
    "sync_projection",
    "sync_zone",
    "light_geometry",
    "govee_models",
    "virtual_lights",
)


def endpoints(config):
    """Where Light Studio and the host reach each component.

    The adapter listens on the core address of the container's internal network;
    the bridge answers on every address of the container.
    """
    address = config.container.core_address
    return {
        "adapter": f"http://{address}:{config.port}",
        "setup": (address, config.setup.port),
        "bridge": f"http://{address}",
    }
