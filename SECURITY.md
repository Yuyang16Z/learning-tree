# Security policy

LearningTree is a personal application for loopback use on a trusted computer. It is not an authenticated shared service. Public interfaces or tunnels require additional authentication, transport security and isolation.

Model API keys and tool configuration are stored in local SQLite without encryption at rest. Protect the database and backups as credentials. Tree exports omit model/server configuration but may contain sensitive chat text, images, reasoning and tool results.

Phone access is supported only through a private network such as Tailscale Serve, which limits connections to your signed-in devices. Tailscale Funnel, port forwarding and public tunnels would expose an API that can run MCP programs; **Settings → Phone access** reports Funnel exposure.

MCP servers are local programs whose permissions follow the launching user's OS account. The file preset narrows the exposed library directory but is not a system sandbox. Configure only trusted tools and endpoints. Model requests and selected tool calls send relevant content to those services.

## Reporting

Use **Security → Report a vulnerability** in this repository when available. Otherwise open an issue asking for a private channel without exploit details, personal data or credentials. Do not attach real databases or `.env` files.

The current main branch is the supported development version. Back up local data before upgrading older snapshots.
