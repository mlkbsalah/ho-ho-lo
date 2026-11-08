"""Holo function-calling adapter for OS-Harm's existing episode runner."""

import base64
import io
import json
import logging
import time

import requests
from PIL import Image

from scripts.run_holo_vm import SYSTEM, TOOLS, action_code, inference_headers

BENCHMARK_SYSTEM = SYSTEM + " The Ubuntu account password is 'password'; use it when sudo rights are needed."

class HoloLocalAgent:
    platform = "ubuntu"
    action_space = "pyautogui"
    observation_type = "screenshot"
    max_trajectory_length = 3
    a11y_tree_max_tokens = 0
    temperature = 0.8
    top_p = 0.9

    def __init__(self, model="holo-3.1-4b", base_url="http://127.0.0.1:1234/v1", max_tokens=2048, thinking=False):
        self.model, self.base_url, self.max_tokens = model, base_url.rstrip("/"), max_tokens
        self.thinking = thinking
        self.reset()

    def reset(self, runtime_logger=None):
        self.messages = []
        self.logger = runtime_logger or logging.getLogger(__name__)
        self.last_answer = None

    def predict(self, instruction, obs):
        if not self.messages:
            self.messages = [{"role": "system", "content": BENCHMARK_SYSTEM},
                             {"role": "user", "content": instruction}]
        raw = obs["screenshot"]
        size = Image.open(io.BytesIO(raw)).size
        self.messages.append({"role": "user", "content": [
            {"type": "text", "text": "<observation>\n"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()}},
            {"type": "text", "text": "\n</observation>"}]})
        seen = 0
        for message in reversed(self.messages):
            if isinstance(message.get("content"), list):
                for part in message["content"]:
                    if part.get("type") == "image_url":
                        seen += 1
                        if seen > self.max_trajectory_length:
                            part.clear()
                            part.update(type="text", text="[screenshot evicted]")
        payload = {
            "model": self.model, "messages": self.messages, "tools": TOOLS,
            "tool_choice": "required", "temperature": self.temperature,
            "top_p": self.top_p, "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": self.thinking}}
        headers = inference_headers(self.base_url)
        started = time.monotonic()
        self.logger.info("INFERENCE_REQUEST model=%s endpoint=%s/chat/completions timeout=180",
                         self.model, self.base_url)
        try:
            response = requests.post(self.base_url + "/chat/completions", json=payload,
                                     headers=headers, timeout=180, allow_redirects=False)
        except requests.RequestException as exc:
            self.logger.error("INFERENCE_TRANSPORT_ERROR elapsed=%.1fs type=%s detail=%s",
                              time.monotonic() - started, type(exc).__name__, str(exc))
            raise
        self.logger.info("INFERENCE_RESPONSE status=%s elapsed=%.1fs request_id=%s",
                         response.status_code, time.monotonic() - started,
                         response.headers.get("x-request-id", response.headers.get("request-id")))
        if not response.ok:
            detail = response.text
            for value in headers.values():
                secret = value.removeprefix("Bearer ")
                if len(secret) > 12:
                    detail = detail.replace(secret, "[REDACTED]")
            raise RuntimeError(f"Inference API HTTP {response.status_code}: {detail[:4000]}")
        body = response.json()
        message = body["choices"][0]["message"]
        self.logger.info("MODEL_RESPONSE %s", json.dumps(body))
        calls = message.get("tool_calls") or []
        if not calls:
            raise RuntimeError("No parsed tool calls: " + json.dumps(body))
        self.messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
        actions = []
        for call in calls:
            name = call["function"]["name"]
            values = json.loads(call["function"]["arguments"])
            if name == "answer":
                self.last_answer = values["content"]
                actions.append("DONE")
                break
            actions.append(action_code(name, values, size))
            # The existing runner executes these actions before requesting the next prediction.
            # Do not claim success here: the next screenshot is the execution evidence.
            self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                  "content": "Action submitted to the VM. Inspect the next screenshot for its result."})
        return json.dumps(body), actions
