"""AE Tool Registry — proxy to the loaded AE package.

The kernel does not own tools. Tools are defined in the AE package
(e.g., base-pmad-ae) and registered via the AE's register() function.
This module provides backward-compatible access to the AE's tool
registry for code that imports from app.tools.

The actual tool implementations live in the AE pip package, NOT here.
"""

import logging

_log = logging.getLogger("pmad_template.tools")


def _get_ae_registration() -> dict:
    """Get the current AE registration from the package registry."""
    from app.package_registry import get_ae_registration
    reg = get_ae_registration()
    if reg is None:
        _log.warning("No AE package loaded — tool registry is empty")
        return {}
    return reg


def get_tool_registry() -> dict:
    """Return the TOOL_REGISTRY dict from the loaded AE package."""
    return _get_ae_registration().get("tools", {})


def get_admin_tools() -> set:
    """Return the ADMIN_TOOLS set from the loaded AE package."""
    return _get_ae_registration().get("admin_tools", set())


def get_tools_for_model(model_name: str, tool_names: list[str]) -> list:
    """Return tool callables for the given model, filtered by allowed names.

    Delegates to the AE's get_tools_for_model if available,
    otherwise falls back to local filtering.
    """
    reg = _get_ae_registration()
    ae_get_tools = reg.get("get_tools_for_model")
    if ae_get_tools is not None:
        return ae_get_tools(model_name, tool_names)

    # Fallback: local filtering
    registry = reg.get("tools", {})
    admin = reg.get("admin_tools", set())
    tools = []
    for name in tool_names:
        if name in admin and model_name != "host":
            continue
        tool_fn = registry.get(name)
        if tool_fn is not None:
            tools.append(tool_fn)
    return tools


# Backward compatibility — these read from the AE at access time
# so they reflect whatever AE is currently loaded (supports hot-swap).
class _LazyRegistry(dict):
    """Dict that delegates to the AE registration on every access."""
    def __getitem__(self, key):
        return get_tool_registry()[key]
    def __contains__(self, key):
        return key in get_tool_registry()
    def __iter__(self):
        return iter(get_tool_registry())
    def __len__(self):
        return len(get_tool_registry())
    def keys(self):
        return get_tool_registry().keys()
    def values(self):
        return get_tool_registry().values()
    def items(self):
        return get_tool_registry().items()
    def get(self, key, default=None):
        return get_tool_registry().get(key, default)


TOOL_REGISTRY = _LazyRegistry()
ADMIN_TOOLS = property(lambda self: get_admin_tools())
