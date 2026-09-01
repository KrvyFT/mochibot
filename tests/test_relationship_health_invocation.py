"""End-to-end reachability of the relationship health tools.

The direct-execute tests prove the handler works. These prove Main can
actually get to it: the tools are on_demand, so they only exist for the model
after a request_tools round trip, and a schema the provider rejects or a
routing table that never maps the name would both fail silently in production.
"""

import json

import pytest

import mochi.skills as skill_registry
from mochi.request_tools import build_catalog


def _skill_for(tool_name: str):
    """Resolve a tool name to its skill instance the way the tool loop does."""
    skill = skill_registry.get_skill(skill_registry.skill_for_tool(tool_name))
    assert skill is not None, f"no skill registered for {tool_name}"
    return skill


def test_skill_is_offered_in_the_requestable_catalog():
    catalog = build_catalog(transport="telegram", user_id=1)
    assert "relationship_health" not in catalog.unavailable, (
        catalog.unavailable.get("relationship_health")
    )
    namespace = catalog.eligible.get("relationship_health")
    assert namespace is not None
    assert set(namespace.tool_names) == {
        "assess_relationship_health",
        "relationship_health_history",
    }


def test_both_tools_route_back_to_this_skill():
    for tool in ("assess_relationship_health", "relationship_health_history"):
        assert skill_registry.skill_for_tool(tool) == "relationship_health"


@pytest.mark.parametrize("tool_name", [
    "assess_relationship_health",
    "relationship_health_history",
])
def test_tool_schema_is_provider_safe(tool_name):
    """Guard the shape an OpenAI-compatible endpoint will accept."""
    tools = {
        tool["function"]["name"]: tool
        for tool in skill_registry.get_tools_by_load("on_demand")
    }
    schema = tools[tool_name]["function"]
    assert schema["name"] == tool_name
    assert schema["description"], "a tool with no description is unpickable"

    params = schema["parameters"]
    assert params["type"] == "object"
    for name, prop in params["properties"].items():
        assert prop.get("description"), f"{name} has no description"
        assert prop["type"] in ("string", "number", "integer", "array")
        # Every array must carry an items schema or the request is rejected.
        if prop["type"] == "array":
            assert "items" in prop
    for name in params.get("required", []):
        assert name in params["properties"]

    # Must survive a JSON round trip; the schema is sent over the wire.
    assert json.loads(json.dumps(schema)) == schema


def test_dimension_items_schema_names_both_fields_as_required():
    tools = {
        tool["function"]["name"]: tool
        for tool in skill_registry.get_tools_by_load("on_demand")
    }
    items = (
        tools["assess_relationship_health"]["function"]["parameters"]
        ["properties"]["dimensions"]["items"]
    )
    assert items["type"] == "object"
    assert set(items["properties"]) == {"dimension", "score"}
    assert items["properties"]["score"]["type"] == "number"
    assert set(items["required"]) == {"dimension", "score"}
    assert items["additionalProperties"] is False


def test_every_valid_dimension_key_is_documented_in_the_schema():
    """The items schema cannot hold an enum, so the key list lives in prose.

    If a dimension were added to the model without updating that prose, the
    model would have no way to learn the new key.
    """
    from mochi.relationship_model import RQI_WEIGHTS

    tools = {
        tool["function"]["name"]: tool
        for tool in skill_registry.get_tools_by_load("on_demand")
    }
    description = (
        tools["assess_relationship_health"]["function"]["parameters"]
        ["properties"]["dimensions"]["description"]
    )
    for key in RQI_WEIGHTS:
        assert key in description, f"{key} is not documented for the model"


@pytest.mark.asyncio
async def test_dispatch_through_the_registry_reaches_the_handler():
    """Invoke the way the tool loop does, not by importing the class."""
    from mochi.skills.base import SkillContext

    skill = _skill_for("assess_relationship_health")
    result = await skill.run(SkillContext(
        trigger="tool_call",
        user_id=1,
        channel_id=1,
        transport="telegram",
        actor="main",
        source="chat",
        turn_id="turn-dispatch",
        tool_name="assess_relationship_health",
        args={
            "subject": "我和小雨",
            "dimensions": [
                {"dimension": "communication_quality", "score": 8},
                {"dimension": "emotional_intimacy", "score": 7},
                {"dimension": "conflict_resolution_capacity", "score": 6},
                {"dimension": "love_language_alignment", "score": 7},
            ],
            "attachment_self": "secure",
            "attachment_other": "avoidant",
        },
    ))
    assert result.success
    assert result.entity_refs and result.state_changed
    # 0.20*8 + 0.20*7 + 0.15*6 + 0.15*7 = 1.6+1.4+0.9+1.05 = 4.95 over
    # coverage 0.70 => 7.0714...; ACS 0.70 => modifier 0.99 => 7.00.
    assert "RQI 7.0/10" in result.output


def test_schema_is_stable_across_repeated_discovery():
    """Discovery runs at startup; a schema that mutates per call would drift."""
    first = json.dumps(
        _skill_for("assess_relationship_health").get_tools(), sort_keys=True,
    )
    second = json.dumps(
        _skill_for("assess_relationship_health").get_tools(), sort_keys=True,
    )
    assert first == second
