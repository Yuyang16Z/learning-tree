# Security policy

LearningTree is a personal application for loopback use on a trusted computer. It is not an authenticated shared service. Public interfaces or tunnels require additional authentication, transport security and isolation.

Model API keys and tool configuration are stored in local SQLite without encryption at rest. Protect the database and backups as credentials. Tree exports omit model/server configuration but may contain sensitive chat text, images, reasoning and tool results.

Phone access is supported only through a private network such as Tailscale Serve, which limits connections to your signed-in devices. Tailscale Funnel, port forwarding and public tunnels would expose an API that can run MCP programs; **Settings → Phone access** reports Funnel exposure.

## Built-in request protections

These narrow common browser and tool attacks; they do not make the API safe to publish.

- **Host check (DNS rebinding).** Requests must use a loopback host name, a Tailscale Serve name (`*.ts.net`) or a name listed in `ALLOWED_HOSTS` (comma-separated). A page whose domain was rebound to 127.0.0.1 still sends its own Host header and gets `400`.
- **Cross-site writes.** `POST`, `PUT`, `PATCH` and `DELETE` with an `Origin` header must come from a local page (any port) or the same origin. CORS alone only stops a foreign page from reading a response, not from triggering a write. Requests without `Origin`, such as scripts, are still accepted.
- **Fetch tool (SSRF).** The built-in `fetch` tool accepts only `http`/`https` URLs without credentials. Every address the host resolves to must be public — loopback, private, link-local (including cloud metadata), shared CGNAT (including Tailscale) and reserved ranges are refused — and each redirect is checked again, up to five. The request connects to the address that was checked, while HTTPS still verifies the certificate for the original host name. This applies only to the built-in tool; MCP servers, including the web-research preset, make their own requests.
- **Question images.** `/ask` accepts at most four images of up to 4 MiB each, matching the composer.

MCP servers are local programs whose permissions follow the launching user's OS account. The file preset narrows the exposed library directory but is not a system sandbox. Configure only trusted tools and endpoints. Model requests and selected tool calls send relevant content to those services.

## Reporting

Use **Security → Report a vulnerability** in this repository when available. Otherwise open an issue asking for a private channel without exploit details, personal data or credentials. Do not attach real databases or `.env` files.

The current main branch is the supported development version. Back up local data before upgrading older snapshots.
