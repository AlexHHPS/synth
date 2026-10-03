# REST and MCP

The Integrations screen displays the approved backend of this build. For a cloud
installation use `https://voice.example.com/v1` and
`https://voice.example.com/mcp`, replacing the example with your actual deployment.
The Mac does not need to be running for another system to read the central library.

Create a key in Integrations and select the folders it can read. It is shown once.
Send `Authorization: Bearer <key>` for both REST and MCP. Employees can use their
active Supabase access token instead. Requests without valid credentials return
401; a resource outside the caller's scope returns 404; integration writes return
403. Revocation takes effect on subsequent requests.

MCP uses authenticated, stateless Streamable HTTP. Configure an MCP client with
the `/mcp` URL and Bearer header. The supported tools are `list_folders`,
`list_meetings`, `get_meeting`, `get_transcript`, `get_document`, `get_summary`,
`get_notes`, `list_versions`, `get_processing_status` and `search_transcripts`.
OAuth discovery for third-party MCP clients is not implemented; use scoped keys.

REST mirrors these tools: GET `/v1/folders`, `/v1/library`,
`/v1/meetings/{id}`, its `/transcript`, `/document`, `/summary`, `/notes`,
`/versions`, `/jobs`, and `/v1/search?q=...`. Current document/summary must match
the current transcript; while it is unavailable these routes return 404.
Explicit `?version=...` reads retained history. Listing accepts `limit`, `after`
and `folder_id`. No tool exposes audio or the voice profile catalog.
