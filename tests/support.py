"""Test-side `Client` pinned to the protocol era the target host speaks.

FastMCP 4's `Client` defaults to `mode="auto"`, which negotiates the sessionless
MCP 2026-07-28 era against this server. The Codex CLI negotiates the 2025-11-25
handshake era, and the two differ in exactly the way this server cares about:
only a handshake connection can be asked for MCP roots, so only there does an
omitted `workspace_root` resolve to the client's first root. Defaulting the
suite to `mode="legacy"` keeps every existing test exercising the host's actual
era; tests about the sessionless era say `mode="auto"` explicitly."""

import fastmcp


def Client(*args, mode="legacy", **kwargs):
    return fastmcp.Client(*args, mode=mode, **kwargs)
