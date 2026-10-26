"""Small loopback probe; bypass system proxy settings and never fetch stream keys."""
import http.client
import json


def probe_http(port, timeout=2):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/api/health")
        response = connection.getresponse()
        body = response.read(4096)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            payload = {}
        return {"status": response.status, "service": payload.get("service", "")
                if isinstance(payload, dict) else ""}
    except http.client.HTTPException as exc:
        raise ValueError(f"Phản hồi HTTP không hợp lệ: {exc}") from exc
    finally:
        connection.close()
