#!/usr/bin/env python3

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock


USAGE = {
    "input_tokens": 0,
    "input_tokens_details": None,
    "output_tokens": 0,
    "output_tokens_details": None,
    "total_tokens": 0,
}


def event_stream(events: list[dict[str, object]]) -> bytes:
    chunks = []
    for event in events:
        chunks.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
    return "".join(chunks).encode()


class ResponsesHandler(BaseHTTPRequestHandler):
    target = ""
    request_count = 0
    lock = Lock()

    def do_POST(self) -> None:
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length))
        with self.lock:
            type(self).request_count += 1
            request_number = type(self).request_count
        if request_number == 1:
            tool_names = {
                tool.get("name")
                for tool in payload.get("tools", [])
                if isinstance(tool, dict)
            }
            if "rt_httpx" not in tool_names:
                self.send_error(400, "rt_httpx was not registered")
                return
            events = [
                {"type": "response.created", "response": {"id": "riftx-response-1"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "riftx-httpx-call",
                        "name": "rt_httpx",
                        "arguments": json.dumps({"targets": [self.target]}),
                    },
                },
                {
                    "type": "response.completed",
                    "response": {"id": "riftx-response-1", "usage": USAGE},
                },
            ]
        else:
            events = [
                {"type": "response.created", "response": {"id": "riftx-response-2"}},
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": "riftx-message-1",
                        "content": [{"type": "output_text", "text": "Recon complete."}],
                    },
                },
                {
                    "type": "response.completed",
                    "response": {"id": "riftx-response-2", "usage": USAGE},
                },
            ]
        body = event_stream(events)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--target", required=True)
    args = parser.parse_args()
    ResponsesHandler.target = args.target
    ThreadingHTTPServer(("127.0.0.1", args.port), ResponsesHandler).serve_forever()


if __name__ == "__main__":
    main()
