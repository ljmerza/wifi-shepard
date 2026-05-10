"""One-shot probe: dump UniFi raw client dict keys to find a BTM capability discriminator.

Usage from the wifi-shepard repo root:

    set -a; source wifi-shepard.env; set +a
    uv run python tools/probe_btm_capability.py

Reads config.yaml for controller credentials. Prints, for each wireless client:
- the sorted list of all keys in the raw dict
- any field whose name matches roaming/capability patterns, with its value

Paste the output back to Claude. Disposable; not committed.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# repo root on sys.path so `from wifi_shepard...` resolves without `pip install -e .`
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from wifi_shepard.config import load_config_from_path  # noqa: E402
from wifi_shepard.controllers.unifi import UniFiController  # noqa: E402

CAPABILITY_PATTERNS = ("11", "wnm", "btm", "capab", "roam", "extend", "transition")
MAX_CLIENTS = 5


async def main() -> int:
    config = load_config_from_path(REPO_ROOT / "config.yaml")
    if not config.controllers:
        print("ERROR: no controllers configured in config.yaml", file=sys.stderr)
        return 2
    spec = config.controllers[0]
    if spec.type != "unifi":
        print(f"ERROR: probe expects type=unifi, got type={spec.type!r}", file=sys.stderr)
        return 2

    # UniFi OS devices (UDM, UDM Pro, UCG, Dream Router) listen on 443; older
    # standalone controllers on 8443. ControllerSpec does not yet carry a port
    # field (config-loader gap, separate fix), so the probe reads UNIFI_PORT
    # directly from env. Default 8443 matches UniFiController's own default.
    port = int(os.environ.get("UNIFI_PORT", "8443"))
    controller = UniFiController(
        host=spec.host,
        username=spec.username,
        password=spec.password,
        site=spec.site,
        verify_ssl=spec.verify_ssl,
        port=port,
        name=spec.name,
    )
    await controller.login()
    try:
        unifi = controller._controller()
        await unifi.clients.update()
        wireless = [c for c in unifi.clients.values() if not c.raw.get("is_wired", False)]
        if not wireless:
            print("WARNING: no wireless clients on controller right now")
            return 0
        print(f"FOUND {len(wireless)} wireless clients; dumping first {MAX_CLIENTS}")
        print("=" * 72)
        for i, client in enumerate(wireless[:MAX_CLIENTS]):
            raw = client.raw
            mac = raw.get("mac", "?")
            hostname = raw.get("hostname", raw.get("name", "?"))
            radio = raw.get("radio", "?")
            print(f"\nCLIENT {i}: mac={mac} hostname={hostname!r} radio={radio}")
            print("ALL_KEYS:")
            for key in sorted(raw.keys()):
                print(f"  - {key}")
            print("\nCAPABILITY-LIKE KEYS (with values):")
            cap_keys = [
                k for k in sorted(raw.keys()) if any(p in k.lower() for p in CAPABILITY_PATTERNS)
            ]
            if not cap_keys:
                print("  (none matched)")
            else:
                for key in cap_keys:
                    print(f"  {key} = {raw[key]!r}")
            print("-" * 72)
    finally:
        await controller.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
