"Loopback-only OpenAI-compatible scripted provider for deterministic fault tests."

import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class ControlledProvider:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.requests.append(body)
                if not owner.responses:
                    self.send_error(500, "No scripted response remaining")
                    return
                response = owner.responses.popleft()
                if callable(response):
                    response = response(body)
                omit = response.pop("_omit_usage", False)
                usage_override = response.pop("_usage", None)
                content = response.pop("_raw_content", None)
                if content is None:
                    content = json.dumps(response, ensure_ascii=False)
                result = {
                    "id": "synthetic-call-" + str(len(owner.requests)),
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": "qwen-plus",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": content,
                                "reasoning_content": "NEVER_SAVE_THOUGHT",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
                if not omit:
                    result["usage"] = {
                        "prompt_tokens": 20,
                        "completion_tokens": 30,
                        "total_tokens": 50,
                    }
                if usage_override is not None:
                    result["usage"] = usage_override
                encoded = json.dumps(result, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


def plan(needs_clarification=False):
    return {
        "needs_clarification": needs_clarification,
        "clarification_question": "请确认指标与统计时间范围" if needs_clarification else None,
        "metric_ids": [] if needs_clarification else ["paid_sales"],
        "steps": []
        if needs_clarification
        else [
            {
                "id": "s1",
                "goal": "汇总实付销售额",
                "metric_ids": ["paid_sales"],
                "result_kind": "aggregate",
                "chart": {"kind": "table", "x_column": None, "y_column": None},
            }
        ],
    }


def query(sql="SELECT SUM(paid_amount) AS paid_sales FROM analysis.orders WHERE status='paid'"):
    return {"sql": sql, "params": {}}


def narrative(body):
    payload = json.loads(body["messages"][-1]["content"])["input"]
    fact = next(cell for cell in payload["cells"] if cell["column"] == "paid_sales")
    return {
        "claims": [
            {
                "evidence_id": fact["evidence_id"],
                "column": fact["column"],
                "row_index": fact["row_index"],
                "operation": "cell",
                "value": fact["value"],
                "text_template": "所选范围内实付销售额为 {value}",
                "denominator_evidence_id": None,
            }
        ]
    }
