"""Verify installed MCPs using public pages and temporary learning-only data."""

import concurrent.futures
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app import mcp_client

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
SPECS = {
    item["key"]: mcp_client.server_spec(
        str(Path(sys.executable).absolute()),
        [str(Path(__file__).with_name("launch.py")), item["key"]],
    )
    for item in CATALOG
}


def main():
    report = {"verified_at": datetime.now(timezone.utc).isoformat(), "servers": [], "checks": []}
    token = "mcp-check-" + uuid.uuid4().hex[:10]
    file = ROOT / "learning-materials" / f"{token}.txt"
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {
                pool.submit(mcp_client.list_tools, SPECS[item["key"]]): item for item in CATALOG
            }
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                tools = future.result()
                assert tools, item["key"]
                report["servers"].append(
                    {
                        **item,
                        "connected": True,
                        "tool_count": len(tools),
                        "tools": [tool["name"] for tool in tools],
                    }
                )
                print(f"{item['label']}: {len(tools)} 个工具", flush=True)

        def call(server, tool, args):
            result = mcp_client.call_tool(SPECS[server], tool, args)
            assert not result.startswith(("MCP 工具报告错误", "MCP 工具 ")), result[:500]
            return result

        result = call("time", "get_current_time", {"timezone": "UTC"})
        assert "UTC" in result
        report["checks"].append("时间工具返回真实时区信息")
        result = call("web-research", "web_search", {"query": "IANA example domains"})
        assert "https://" in result, result[:500]
        report["checks"].append("联网搜索返回真实网页链接")
        result = call(
            "web-research", "read_webpage", {"url": "https://example.com", "max_length": 1500}
        )
        assert "documentation examples" in result, result[:500]
        report["checks"].append("网页阅读获取真实网页正文")
        call("filesystem", "write_file", {"path": str(file), "content": token})
        assert token in call("filesystem", "read_text_file", {"path": str(file)})
        denied = mcp_client.call_tool(SPECS["filesystem"], "list_directory", {"path": str(ROOT)})
        assert any(
            text in denied.lower()
            for text in ["access denied", "outside allowed", "not allowed", "拒绝", "错误"]
        )
        report["checks"].append("学习资料可读写，项目根目录越界访问被拒绝")
        call(
            "memory",
            "create_entities",
            {
                "entities": [
                    {"name": token, "entityType": "安装测试", "observations": ["临时连通性记录"]}
                ]
            },
        )
        assert token in call("memory", "search_nodes", {"query": token})
        mcp_client.invalidate(SPECS["memory"])
        assert token in call("memory", "search_nodes", {"query": token})
        call("memory", "delete_entities", {"entityNames": [token]})
        report["checks"].append("知识记忆重连后保留，临时测试记录已移除")
        call(
            "sequential-thinking",
            "sequentialthinking",
            {
                "thought": "连通性测试第一个检查点。",
                "thoughtNumber": 1,
                "totalThoughts": 2,
                "nextThoughtNeeded": True,
            },
        )
        thought = json.loads(
            call(
                "sequential-thinking",
                "sequentialthinking",
                {
                    "thought": "连通性测试完成。",
                    "thoughtNumber": 2,
                    "totalThoughts": 2,
                    "nextThoughtNeeded": False,
                },
            )
        )
        assert thought["thoughtHistoryLength"] >= 2
        report["checks"].append("分步思考跨调用保留步骤状态")
        call("playwright", "browser_navigate", {"url": "https://example.com"})
        assert "Example Domain" in call("playwright", "browser_snapshot", {})
        call("playwright", "browser_close", {})
        report["checks"].append("浏览器先导航再快照，页面状态连续保留")
        report["all_passed"] = True
    finally:
        file.unlink(missing_ok=True)
        # Only remove the uniquely named entity created by this verification.
        try:
            mcp_client.call_tool(SPECS["memory"], "delete_entities", {"entityNames": [token]})
        except Exception:
            pass
        mcp_client.close_all()
        report["servers"].sort(key=lambda item: list(SPECS).index(item["key"]))
        path = ROOT / "artifacts/mcp-installation.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"all_passed": report.get("all_passed", False), "checks": report["checks"]},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
