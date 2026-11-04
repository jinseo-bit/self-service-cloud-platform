"""MCP server for the Self-Service Cloud Platform (D-180, D-181). Run by Claude Code over stdio (see .mcp.json).

Sign in first, in your own terminal: python -m agent.login --help
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from agent import tools
from agent.platform_client import PlatformError

INSTRUCTIONS = """Self-Service Cloud Platform agent. You help a developer deploy their repository.
Rules:
1. Start with analyze_repo. Show the developer each value with its evidence. Ask them to confirm every item in
   `confirm`, and ask every question in `questions`. Never guess an answer the code did not give.
2. Call recommend with the completed requirements and explain the run model, reasons, cost and anything not chosen.
3. create_pending_environment only makes a DRAFT. You cannot approve, delete, retry, redeploy, or agree to exceed the
   budget: tell the developer to do that themselves in the web UI (`approve_here`).
4. Never put passwords, keys or tokens in env_vars; the platform rejects secret-like values.
5. If something fails or drifts (status DRIFTED), call explain_failure and relay `what_to_do`. Do not invent causes.
   A repair is a person's click on '원래 구성으로 복구' in the web UI."""

mcp = FastMCP("sscp", instructions=INSTRUCTIONS)


def _safe(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except PlatformError as e:
        return {"error": e.message, "status": e.status, **({"detail": e.detail} if e.detail else {})}


@mcp.tool()
def analyze_repo(path: str) -> dict:
    """Read a local repository and draft the platform's requirement answers, each with the file that justifies it.
    Returns `confirm` (inferred answers the developer must confirm), `questions` (what code cannot tell) and `draft`."""
    return _safe(tools.analyze_repo, path)


@mcp.tool()
def recommend(requirements: dict) -> dict:
    """Ask the platform engine which architecture fits the completed requirements (the `draft` from analyze_repo with
    every None filled in). Read-only: creates nothing."""
    return _safe(tools.recommend, requirements)


@mcp.tool()
def create_pending_environment(name: str, requirements: dict, image: str | None = None, health_path: str = "/",
                               env_vars: dict | None = None) -> dict:
    """Create a DRAFT environment. Nothing is built in AWS until a person approves it in the web UI.
    For image-based apps pass `image` (with a tag or digest), `health_path` and non-secret `env_vars`."""
    return _safe(tools.create_pending_environment, name, requirements, image, health_path, env_vars)


@mcp.tool()
def upload_site(env_id: str, path: str) -> dict:
    """Upload a static site build folder (or .zip, index.html inside) into a DRAFT environment."""
    return _safe(tools.upload_site, env_id, path)


@mcp.tool()
def get_environment(env_id: str) -> dict:
    """Status, URL, check counts and recent events of one environment."""
    return _safe(tools.get_environment, env_id)


@mcp.tool()
def list_environments() -> dict:
    """The developer's environments that are not deleted."""
    return _safe(tools.list_environments)


@mcp.tool()
def recheck_environment(env_id: str) -> dict:
    """Run a running environment's security and health checks again now. Read-only: a difference from the approved
    configuration is reported (status DRIFTED with a repair plan), never changed back by you."""
    return _safe(tools.recheck_environment, env_id)


@mcp.tool()
def explain_failure(env_id: str) -> dict:
    """Why an environment failed (stage and failed checks) and what the developer can change."""
    return _safe(tools.explain_failure, env_id)


if __name__ == "__main__":
    mcp.run()
