"""Read the backend address and optionally open it, without shell interpolation."""
import ipaddress
from pathlib import Path
import re
import sys
import time
import webbrowser


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def browser_url():
    # This loader only imports configuration dependencies, never the app/server.
    from src.config.app_config import AppConfig

    config = AppConfig.load()
    if not 1 <= config.port <= 65535:
        raise ValueError("Port must be between 1 and 65535")
    host = config.host
    if host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = "[" + str(ipaddress.IPv6Address(host)).replace("%", "%25") + "]"
    else:
        host = host.encode("idna").decode("ascii")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            raise ValueError("Invalid server host")
    return f"http://{host}:{config.port}"


def main():
    try:
        url = browser_url()
    except Exception:
        print("  Error: Cannot read server URL. Check config/app.yaml and run init.bat or bash init.sh.",
              file=sys.stderr)
        return 1
    if "--open" in sys.argv:
        time.sleep(3)
        if not webbrowser.open(url):
            print("  Browser could not be opened automatically. Open " + url)
    else:
        print("  Server starting at " + url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
