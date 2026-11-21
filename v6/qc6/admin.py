"""Workspace administration from the command line (for proxy / OIDC sign-in, where nobody is created automatically).

    python -m qc6.admin create-workspace "Acme Digital" priya@acme.com "Priya Nair"
    python -m qc6.admin add-member 1 rahul@acme.com "Rahul Mehta" editor
    python -m qc6.admin list
"""
from __future__ import annotations

import asyncio
import sys

from qc6 import services
from qc6.db.migrate import migrate
from qc6.db.pool import close_pool, get_pool


async def main(argv) -> int:
    await migrate()
    pool = await get_pool()
    try:
        if argv[:1] == ["create-workspace"] and len(argv) == 4:
            out = await services.create_workspace(pool, argv[1], argv[2], argv[3])
            print(f"Workspace {out['workspace_id']} created; {argv[2]} is its owner.")
        elif argv[:1] == ["add-member"] and len(argv) in (4, 5):
            role = argv[4] if len(argv) == 5 else "editor"
            if role not in ("owner", "editor", "viewer"):
                print("Role must be owner, editor or viewer.")
                return 2
            mid = await services.add_member(pool, int(argv[1]), argv[2], argv[3], role)
            print(f"{argv[2]} added as {role} (member {mid}).")
        elif argv[:1] == ["list"]:
            for r in await pool.fetch("SELECT w.id, w.name, m.email, m.role FROM workspaces w LEFT JOIN members m ON m.workspace_id = w.id ORDER BY w.id, m.id"):
                print(f"{r['id']:>4}  {r['name']:<30}  {r['email'] or '':<35}  {r['role'] or ''}")
        else:
            print(__doc__)
            return 2
        return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
