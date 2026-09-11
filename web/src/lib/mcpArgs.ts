import { translate } from "../i18n";

/** Keep each MCP argument intact, including spaces and non-ASCII paths. */
export function parseMcpArgs(text: string): string[] {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() || "[]");
  } catch {
    throw new Error(translate('启动参数格式有误，请填写 JSON 数组，例如 ["mcp-server-fetch"]。', 'Invalid arguments. Enter a JSON array, for example ["mcp-server-fetch"].'));
  }
  if (!Array.isArray(parsed) || !parsed.every((arg) => typeof arg === "string")) {
    throw new Error(translate("启动参数必须是字符串数组；无参数时填写 []。", "Arguments must be a string array. Use [] for no arguments."));
  }
  return parsed;
}
