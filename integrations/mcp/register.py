"""Register only our managed, local MCP presets. Existing custom servers stay intact."""

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8099"


def api(path, body=None, method=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def is_managed_preset(server: dict, key: str) -> bool:
    args = server.get("args") or []
    return (
        len(args) == 2
        and isinstance(args[0], str)
        and args[0].replace("\\", "/").endswith("/integrations/mcp/launch.py")
        and args[1] == key
    )


def main():
    try:
        if api("/health").get("name") != "学习树":
            raise RuntimeError("8099 端口不是学习树服务")
    except Exception:
        raise SystemExit("请先启动学习树（8099），再运行 integrations/mcp/register.py。") from None
    subprocess.run([sys.executable, str(ROOT / "scripts/backup_data.py")], cwd=ROOT, check=True)
    catalog = json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))
    servers = api("/mcp")
    for item in catalog:
        previous = next(
            (s for s in servers if is_managed_preset(s, item["key"])),
            None,
        )
        payload = {
            "label": item["label"],
            "command": str(Path(sys.executable).absolute()),
            "args": [str(Path(__file__).with_name("launch.py")), item["key"]],
            "enabled": previous["enabled"] if previous else True,
        }
        if previous:
            api(f"/mcp/{previous['id']}", payload, "PUT")
        else:
            api("/mcp", payload, "POST")
        print(f"已配置：{item['label']}")


if __name__ == "__main__":
    main()
