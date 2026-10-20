"""Clubs connecting AI assistants (Claude, ChatGPT, Cursor...) over MCP.

- `oauth`: the authorization server for the MCP endpoint (OAuth 2.1, PKCE,
  dynamic registration, client metadata documents, refresh rotation).
- `pages`: the sign-in, club and consent pages a person sees while connecting.
- `server`: the MCP endpoint itself, /mcp, on the official SDK.
- `tools`: what an assistant can do, each tool checked against the token's
  scopes, the club's plan and the person's current role on every call.
- `actions`: what an assistant may only ask for -- publishing an event,
  sending an announcement -- held until a person approves it in the dashboard.

docs/MCP.md has the whole picture; APP_MCP_ENABLED turns it on.
"""
