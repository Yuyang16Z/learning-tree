"""Register only our managed, local MCP presets. Existing custom servers stay intact."""

import argparse
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BASE = "http://127.0.0.1:8099"
PREVIOUS_KEYS = {"web-research": ("fetch",)}
PREVIOUS_LABELS = {"web-research": ("网页阅读", "Web reader", "Web reading")}


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


def main(argv=()):
    catalog = json.loads((Path(__file__).with_name("catalog.json")).read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", choices=[item["key"] for item in catalog])
    options = parser.parse_args(argv)
    if options.preset:
        catalog = [item for item in catalog if item["key"] == options.preset]
    try:
        if api("/health").get("name") != "学习树":
            raise RuntimeError("8099 端口不是学习树服务")
    except Exception:
        raise SystemExit("请先启动学习树（8099），再运行 integrations/mcp/register.py。") from None
    subprocess.run([sys.executable, str(ROOT / "scripts/backup_data.py")], cwd=ROOT, check=True)
    servers = api("/mcp")
    for item in catalog:
        previous = next(
            (
                s
                for key in (item["key"], *PREVIOUS_KEYS.get(item["key"], ()))
                for s in servers
                if is_managed_preset(s, key)
            ),
            None,
        )
        label = previous["label"] if previous else item["label"]
        if label in PREVIOUS_LABELS.get(item["key"], ()):
            label = item["label"]
        payload = {
            "label": label,
            "command": str(Path(sys.executable).absolute()),
            "args": [str(Path(__file__).with_name("launch.py").resolve()), item["key"]],
            "enabled": previous["enabled"] if previous else True,
        }
        if previous:
            api(f"/mcp/{previous['id']}", payload, "PUT")
        else:
            api("/mcp", payload, "POST")
        print(f"已配置：{item['label']}")


if __name__ == "__main__":
    main(sys.argv[1:])
