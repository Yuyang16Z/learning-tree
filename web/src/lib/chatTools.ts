import type { McpServer } from "../types";

/** Identify managed presets by their launcher, never by an editable display name. */
export function managedMcpPreset(server: McpServer): string | undefined {
  const launcher = server.args[0]?.replace(/\\/g, "/");
  return launcher?.endsWith("/integrations/mcp/launch.py") ? server.args[1] : undefined;
}

export function hasManagedWebSearch(servers: readonly McpServer[]): boolean {
  return servers.some(server => server.enabled && managedMcpPreset(server) === "web-research");
}

/** Build the allowlist for a question; availability never grants permission. */
export function selectedChatTools(
  servers: readonly McpServer[],
  selection: { useSearch: boolean; useFetch: boolean; selectedMcp: readonly number[] },
): string[] {
  const useBuiltins = !hasManagedWebSearch(servers);
  const enabled = new Set(servers.filter(server => server.enabled).map(server => server.id));
  return [
    ...(useBuiltins && selection.useSearch ? ["web_search"] : []),
    ...(useBuiltins && selection.useFetch ? ["fetch"] : []),
    ...[...new Set(selection.selectedMcp)].filter(id => enabled.has(id)).map(id => `mcp_server_${id}`),
  ];
}
