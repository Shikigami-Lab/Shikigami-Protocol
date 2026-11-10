"""Registry for agent business tools (the plugin-shaped extension point).

A business tool is a read-mostly capability served to the native Harness loop
through the per-run MCP bridge and dispatched host-side via
``call_business_tool``. Registering one spec wires all three layers at once:

- dispatch (``src.agent.business_tools.call_business_tool``)
- capability gating (``src.agent.capabilities``)
- MCP serving (``src.agent.mcp_server``)

Third-party tools register a spec from their own module and arrange for that
module to be imported (tool package ``register()`` / future auto-discovery).
Core files never need editing for a new tool.

Wrapper factories live with the tool and import the MCP SDK lazily, so the
data/dispatch layer stays importable in environments without ``mcp``.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from pydantic import BaseModel

# async (app, session, parsed_args) -> result dict ({"ok": ...} shape)
BusinessToolHandler = Callable[[Any, Any, BaseModel], Awaitable[dict]]
# (call_tool) -> async MCP wrapper function with Annotated parameters;
# the wrapper's __name__ must equal the spec name (FastMCP uses it as the
# tool name) and its docstring becomes the MCP tool description.
WrapperFactory = Callable[[Callable], Callable]
# (settings, profile) -> bool; only for tools whose availability is not one of
# the standard capabilities.KEYS switches (e.g. weather reads tool_configs).
CapabilityResolver = Callable[[Any, Any], bool]


@dataclass(frozen=True)
class BusinessToolSpec:
    name: str
    args_model: type
    handler: BusinessToolHandler
    capability: str                                  # access-dict key gating the tool
    wrapper_factory: WrapperFactory                  # MCP adapter, SDK import stays lazy
    capability_resolver: Optional[CapabilityResolver] = None  # non-standard switches only
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False
    # Exception path of call_business_tool: handlers that return an {"ok": False}
    # dict themselves never hit this; uncaught operational errors map here.
    error_code: str = "tool_unavailable"
    error_message: str = "This tool is unavailable right now"


_REGISTRY: dict[str, BusinessToolSpec] = {}
_builtins_loaded = False


def register_business_tool(spec: BusinessToolSpec) -> BusinessToolSpec:
    if not isinstance(spec.name, str) or not spec.name.strip():
        raise ValueError("Business tool spec requires a nonblank name")
    if spec.name.startswith(("timer_", "todo_")):
        raise ValueError("timer_/todo_ tools belong to the runtime family, not this registry")
    if spec.name in _REGISTRY:
        raise ValueError(f"Duplicate business tool registration: {spec.name}")
    if not (isinstance(spec.args_model, type) and issubclass(spec.args_model, BaseModel)):
        raise ValueError("Business tool args_model must be a pydantic BaseModel subclass")
    _REGISTRY[spec.name] = spec
    return spec


def get_business_tool(name: str) -> Optional[BusinessToolSpec]:
    ensure_builtin_tools()
    return _REGISTRY.get(name)


def all_business_tools() -> tuple[BusinessToolSpec, ...]:
    ensure_builtin_tools()
    return tuple(_REGISTRY.values())


def ensure_builtin_tools() -> None:
    """Import the built-in tool modules once so their registrations land."""
    global _builtins_loaded
    if _builtins_loaded:
        return
    _builtins_loaded = True
    import src.agent.business_tools  # noqa: F401  (registers the built-in specs)
