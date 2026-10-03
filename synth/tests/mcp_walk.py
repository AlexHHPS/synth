"""Independent JSON-RPC consumer using an actual folder-scoped machine key."""
import json
from pathlib import Path
import urllib.error
import urllib.request
import uuid

from api_access_walk import ADMIN, BASE, ROOT, call


def main():
    mid = json.loads((ROOT / "synth/.runtime/document-walk.json").read_text())["meeting_id"]
    meeting = call("GET", f"/v1/meetings/{mid}", ADMIN)
    principal = call("POST", "/v1/principals", ADMIN, {"name": "Consumidor MCP " + str(uuid.uuid4())[:8], "kind": "machine"}, 201)
    key = call("POST", "/v1/keys", ADMIN, {"principal_id": principal["id"], "folder_ids": [meeting["folder_id"]]}, 201)
    try:
        def rpc(method, params=None, rid=1):
            request = urllib.request.Request(BASE + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}).encode(),
                headers={"Authorization": "Bearer " + key["token"], "Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            return json.load(urllib.request.urlopen(request, timeout=15))["result"]
        assert rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "synth-test", "version": "1"}})["capabilities"] == {"tools": {}}
        tools = rpc("tools/list")["tools"]
        assert {t["name"] for t in tools} == {"get_document", "get_transcript", "list_meetings", "search_transcripts", "list_folders", "get_meeting", "get_summary", "get_notes", "list_versions", "get_processing_status"}
        rest = call("GET", f"/v1/meetings/{mid}/document", key["token"])
        mcp = json.loads(rpc("tools/call", {"name": "get_document", "arguments": {"meeting_id": mid}})["content"][0]["text"])
        assert rest == mcp
        search = call("GET", "/v1/search?q=piloto", key["token"])
        assert search["items"] and all(row["meeting_id"] == mid for row in search["items"])
        denied = rpc("tools/call", {"name": "get_document", "arguments": {"meeting_id": str(uuid.uuid4())}})
        assert denied["isError"]
        assert rpc("tools/call", {"name": "get_audio", "arguments": {"meeting_id": mid}})["isError"]
        assert rpc("tools/call", {"name": "get_voice_profile", "arguments": {"meeting_id": mid}})["isError"]
        request = urllib.request.Request(BASE + "/mcp", data=b'{"jsonrpc":"2.0","id":2,"method":"ping"}', headers={"Authorization": "Bearer " + key["token"], "Content-Type": "application/json", "Origin": "https://untrusted.example"})
        try:
            urllib.request.urlopen(request, timeout=15)
        except urllib.error.HTTPError as error:
            assert error.code == 403
        else:
            raise AssertionError("untrusted origin accepted")
        report = {"mode": "real", "status": "PASS", "document_hash": rest["content_hash"], "checks": ["rest_consumer", "mcp_consumer", "same_document_hash", "audio_and_profiles_not_exposed", "search_isolation", "direct_id_denied", "untrusted_origin_denied"]}
        (ROOT / "synth/.runtime/mcp-walk.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))
    finally:
        call("DELETE", f'/v1/keys/{key["id"]}', ADMIN)


if __name__ == "__main__":
    main()
