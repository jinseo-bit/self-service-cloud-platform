"""Sign the Claude Code agent in to the platform (D-181). Run this in YOUR terminal, not through the agent:
the password is read with getpass here and is never stored, logged or sent to the MCP server.

  Cloud:  python -m agent.login --email you@example.com --api-url <api_url> --ui-url <ui_url> \
                                --region ap-northeast-2 --client-id <agent_client_id>
          (values: `terraform output` in infra/platform; agent_client_id is not a secret)
  Local:  python -m agent.login --email dev@example.com --local http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import getpass
import sys

from agent.platform_client import CONFIG, PlatformError, login


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m agent.login", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True)
    ap.add_argument("--api-url", default="")
    ap.add_argument("--ui-url", default="")
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--client-id", default="")
    ap.add_argument("--local", default="", help="base URL of scripts/local_dev.py")
    a = ap.parse_args(argv)
    password = None if a.local else getpass.getpass("Password (not echoed, not stored): ")
    try:
        info = login(email=a.email, password=password, api_url=a.api_url, ui_url=a.ui_url, region=a.region,
                     client_id=a.client_id, local=a.local)
    except PlatformError as e:
        print(f"login failed: {e.message}", file=sys.stderr)
        return 1
    print(f"signed in as {info['email']} ({info['mode']}); token saved to {CONFIG}")
    print("The agent can draft and read environments. Approving, deleting and retrying stay in the web UI.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
