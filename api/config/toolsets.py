"""CLI toolset resolution policy shared by run entrypoints."""

from api import config as _config_module


DEFAULT_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "cronjob",
    "delegation",
    "file",
    "image_gen",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "web",
    "webhook",
]

LEGACY_CLI_TOOLSET_ALIASES = {
    # Older Hermes configs used "hermes" as the CLI composite toolset. Modern
    # Hermes Agent exposes that split as these two registered composites.
    "hermes": ("hermes-cli", "hermes-api-server"),
}


def normalize_cli_toolsets(toolsets):
    """Expand legacy CLI aliases while preserving order and removing duplicates."""
    normalized = []
    seen = set()
    for name in toolsets or []:
        replacements = LEGACY_CLI_TOOLSET_ALIASES.get(name, (name,))
        for replacement in replacements:
            if replacement and replacement not in seen:
                seen.add(replacement)
                normalized.append(replacement)
    return normalized


def resolve_cli_toolsets(cfg=None):
    """Resolve platform CLI toolsets, including dynamically registered MCP tools."""
    if cfg is None:
        cfg = _config_module.get_config()
    try:
        from hermes_cli.tools_config import _get_platform_tools

        return normalize_cli_toolsets(_get_platform_tools(cfg, "cli"))
    except Exception:
        configured = cfg.get("platform_toolsets", {}).get("cli", DEFAULT_TOOLSETS)
        return normalize_cli_toolsets(configured)


__all__ = ["normalize_cli_toolsets", "resolve_cli_toolsets"]
