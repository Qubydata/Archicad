"""Run and administer the Archicad MCP cloud service.

    python -m archicad_mcp_cloud serve              start the service (PORT, default 8080)
    python -m archicad_mcp_cloud check              list missing settings
    python -m archicad_mcp_cloud user EMAIL         show an account and its usage
    python -m archicad_mcp_cloud comp EMAIL DAYS    give an account free access, e.g. for beta testers
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="archicad-mcp-cloud")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("check")
    user_parser = sub.add_parser("user")
    user_parser.add_argument("email")
    comp_parser = sub.add_parser("comp")
    comp_parser.add_argument("email")
    comp_parser.add_argument("days", type=int)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from .config import CloudConfig

    config = CloudConfig()
    if args.command == "serve":
        import uvicorn

        from .app import create_app

        uvicorn.run(create_app(config), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")),
                    proxy_headers=True, forwarded_allow_ips="*")
        return 0
    if args.command == "check":
        problems = config.problems()
        for problem in problems:
            print(f"- {problem}")
        print("Configuration looks complete." if not problems else f"{len(problems)} setting(s) to fix.")
        return 1 if problems else 0

    from .db import Database

    db = Database(config.database_path, config.secret_key)
    email = args.email.strip().lower()
    if args.command == "comp":
        user = db.get_or_create_user(email)
        now = time.time()
        db.set_subscription(user.id, None, "comped", now, now + args.days * 86400, False)
        print(f"{email} has free access for {args.days} days.")
        return 0
    user = db.user_by_email(email)
    if not user:
        print(f"No account for {email}.")
        return 1
    since = user.period_start or time.time() - 30 * 86400
    print(f"{user.email}: status {user.status}, customer {user.stripe_customer_id or '-'}, "
          f"period ends {time.strftime('%Y-%m-%d', time.gmtime(user.period_end)) if user.period_end else '-'}, "
          f"AI cost this period ${db.used_micros(user.id, since) / 1e6:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
