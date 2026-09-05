# Install the Logseq DB-Native Skill

This skill is for the `mcp-logseq-db` Claude Desktop server only. It must not be
loaded together with any legacy `logseq-db-graph` or `logseq-file-graph` skill.

Import this `logseq-db-native` folder into Claude Desktop Skills. Restart
Claude Desktop if the skill does not appear immediately, then enable the skill
in a conversation that has the `mcp-logseq-db` connector available.

The skill does not contain an API token. Claude Desktop reads the token from
its local MCP server configuration.