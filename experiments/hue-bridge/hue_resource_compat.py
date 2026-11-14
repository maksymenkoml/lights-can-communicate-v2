"""Correct the pinned emulator's public v2 resource relationships."""

import copy
import uuid


def canonical_collection(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return payload
    return {**payload, "data": [row for row in payload["data"] if row.get("type") != "diyhue"]}


def canonical_light(payload):
    result = copy.deepcopy(payload)
    temperature = result.get("color_temperature")
    if isinstance(temperature, dict) and temperature.get("mirek") is None:
        temperature["mirek_valid"] = False
    if result.get("timed_effects") == {}:
        result["timed_effects"] = {
            "status": "no_effect",
            "status_values": ["no_effect"],
            "effect_values": ["no_effect"],
        }
    effects = result.get("effects")
    if isinstance(effects, dict):
        for field in ("status_values", "effect_values"):
            if isinstance(effects.get(field), list):
                effects[field] = [value for value in effects[field] if value != "colorloop"]
        if effects.get("status") == "colorloop":
            effects["status"] = "no_effect"
    return result


def install_resource_compat(
    api, group_class, bridge_id, groups=None, event_module=None, light_class=None
):
    original_entertainment = api.v2BridgeEntertainment
    original_home = api.v2BridgeHome
    original_geolocation = api.geoLocation
    original_grouped_light = group_class.getV2GroupedLight

    def bridge_entertainment():
        result = original_entertainment()
        result["id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, bridge_id + "entertainment"))
        return result

    def bridge_home():
        result = original_home()
        result["services"] = [
            service for service in result.get("services", []) if service.get("rtype") != "device"
        ]
        return result

    def geolocation():
        result = original_geolocation()
        if not result.get("is_configured"):
            result.pop("sun_today", None)
        return result

    def owner_for(group):
        if group.id_v1 == "0":
            owner = {"rid": api.v2BridgeHome()["id"], "rtype": "bridge_home"}
        elif group.type == "Room":
            owner = {"rid": group.getV2Room()["id"], "rtype": "room"}
        elif group.type == "Zone":
            owner = {"rid": group.getV2Zone()["id"], "rtype": "zone"}
        else:
            owner = {
                "rid": str(
                    uuid.uuid5(uuid.NAMESPACE_URL, group.id_v2 + "entertainment_configuration")
                ),
                "rtype": "entertainment_configuration",
            }
        return owner

    def grouped_light(group):
        result = original_grouped_light(group)
        result["owner"] = owner_for(group)
        return result

    api.v2BridgeEntertainment = bridge_entertainment
    api.v2BridgeHome = bridge_home
    api.geoLocation = geolocation
    group_class.getV2GroupedLight = grouped_light
    if light_class is not None:
        original_light = light_class.getV2Api

        def light_api(light):
            return canonical_light(original_light(light))

        light_class.getV2Api = light_api
    if groups is not None and event_module is not None:
        original_event = event_module.StreamEvent

        def stream_event(message):
            result = copy.deepcopy(message)
            by_id = {group.id_v2: group for group in groups.values()}
            for resource in result.get("data", []):
                group = by_id.get(resource.get("id"))
                if resource.get("type") == "grouped_light" and group is not None:
                    resource["owner"] = owner_for(group)
            return original_event(result)

        event_module.StreamEvent = stream_event
