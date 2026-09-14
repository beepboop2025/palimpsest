"""Exercise the browser's implicit favicon request against the actual server."""
from pathlib import Path
import importlib.util
import threading
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "favicon_server", ROOT / "ops/railway/static_server.py"
)
server_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server_module)


def test_browser_favicon_get_and_head_resolve_to_the_existing_icon(tmp_path):
    icon = (ROOT / "brand/palimpsest-icon.svg").read_bytes()
    (tmp_path / "brand").mkdir()
    (tmp_path / "brand/palimpsest-icon.svg").write_bytes(icon)
    server = server_module.create_server(tmp_path, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for method in ("GET", "HEAD"):
            connection = __import__("http.client", fromlist=["HTTPConnection"]).HTTPConnection(
                "127.0.0.1", server.server_port, timeout=5
            )
            connection.request(method, "/favicon.ico")
            response = connection.getresponse()
            assert response.status == 302
            assert response.getheader("Location") == "/brand/palimpsest-icon.svg"
            assert response.read() == b""
            connection.close()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/favicon.ico", timeout=5
        ) as response:
            assert response.status == 200
            assert response.headers.get_content_type() == "image/svg+xml"
            assert response.read() == icon
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
