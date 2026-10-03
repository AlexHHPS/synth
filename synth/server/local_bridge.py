"""Fixed-destination bridge: isolated router -> native LLM, localhost -> router.

No CONNECT, URL proxying, arbitrary host, redirects, request/content logs or DNS selected
by callers. Keeping the router on an internal network blocks external provider access.
"""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading

MAX_BODY = 16 * 1024 * 1024


def handler(target, native=False):
    class FixedBridge(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def forward(self):
            if not self.path.startswith("/") or self.path.startswith("//"):
                self.send_error(400)
                return
            if native and self.path.split("?", 1)[0] not in ("/health", "/v1/models", "/v1/chat/completions"):
                self.send_error(404)
                return
            if self.headers.get("Transfer-Encoding"):
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self.send_error(400)
                return
            if not 0 <= length <= MAX_BODY:
                self.send_error(413)
                return
            body = self.rfile.read(length)
            headers = {key: value for key, value in self.headers.items()
                       if key.lower() not in ("host", "connection", "content-length", "transfer-encoding", "accept-encoding")}
            headers["Host"] = "127.0.0.1:20129" if not native else "127.0.0.1:18282"
            if native:
                headers["Authorization"] = "Bearer " + Path("/run/secrets/native_llm_key").read_text().strip()
            conn = HTTPConnection(*target, timeout=300)
            try:
                conn.request(self.command, self.path, body=body, headers=headers)
                response = conn.getresponse()
                data = response.read(MAX_BODY + 1)
                if len(data) > MAX_BODY:
                    raise ValueError("response_limit")
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if key.lower() not in ("connection", "content-length", "transfer-encoding", "content-encoding"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (OSError, ValueError):
                self.send_error(502, "Local backend unavailable")
            finally:
                conn.close()

        do_GET = forward
        do_POST = forward
        do_PUT = forward
        do_PATCH = forward
        do_DELETE = forward
        do_OPTIONS = forward

    return FixedBridge


def main():
    native = ThreadingHTTPServer(("0.0.0.0", 18383), handler(("host.docker.internal", 18282), native=True))
    threading.Thread(target=native.serve_forever, daemon=True).start()
    front = ThreadingHTTPServer(("0.0.0.0", 20129), handler(("omniroute", 20128)))
    front.serve_forever()


if __name__ == "__main__":
    main()
