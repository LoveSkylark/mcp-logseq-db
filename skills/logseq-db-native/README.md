# Install the Logseq DB-Native Skill

This skill covers Logseq 2.x DB graphs through either the Python
`mcp-logseq-db` server or Logseq's native MCP server. Their tool surfaces are
not identical: linked-embed tools are native-only, so check the connected
server's available tools before using them. It must not be loaded together
with any legacy `logseq-db-graph` or `logseq-file-graph` skill.

Install this folder using the skill mechanism supported by your agent client.
For Claude Desktop, import the `logseq-db-native` folder under **Settings >
Customize > Skills**, then enable it in a conversation with the relevant
Logseq DB MCP connector. Other clients may use a different discovery path.

The skill does not contain an API token. Keep credentials in the MCP client's
local server configuration.