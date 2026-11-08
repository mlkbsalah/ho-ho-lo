"""Local Holo GUI smoke test against an already-running OS-Harm VM.

Inference runs on the host; all GUI actions execute through the VM HTTP server.
This is a connectivity/capability test, not an OS-Harm evaluation or safety score.
Tool format: https://hub.hcompany.ai/models-api/build-an-agent/function-calling
"""

import argparse
import base64
import datetime
import io
import json
import os
from pathlib import Path
import time
from urllib.parse import urlparse

import requests
from PIL import Image


def inference_headers(base_url):
    """Use H credentials only for the official HTTPS Models API endpoint."""
    parsed = urlparse(base_url)
    if parsed.hostname == "api.hcompany.ai":
        if parsed.scheme != "https":
            raise ValueError("H Company authentication requires HTTPS")
        key = os.environ.get("HAI_API_KEY")
        if not key:
            raise ValueError("Set HAI_API_KEY in the environment or os-harm/.env for the Models API")
        return {"Authorization": "Bearer " + key}
    return {}


def load_credentials(env_file=None):
    root = Path(__file__).resolve().parents[1]
    specified = env_file
    candidates = [Path(specified).expanduser()] if specified else [root / ".env", root.parent / "env.conf"]
    if specified and not candidates[0].is_file():
        raise FileNotFoundError(f"Credential file does not exist: {candidates[0]}")
    if not os.environ.get("HAI_API_KEY") and os.environ.get("H_API_KEY"):
        os.environ["HAI_API_KEY"] = os.environ["H_API_KEY"]
    for env_file in candidates:
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                name, separator, value = line.partition("=")
                name = name.strip().removeprefix("export ")
                if separator and name in ("HAI_API_KEY", "H_API_KEY", "OPENAI_API_KEY"):
                    target = "HAI_API_KEY" if name == "H_API_KEY" else name
                    if os.environ.get(target) and not specified:
                        continue
                    value = value.strip()
                    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                        value = value[1:-1]
                    os.environ[target] = value


def configure_inference(args):
    load_credentials(getattr(args, "env_file", None))
    hosted = args.provider == "hcompany"
    args.base_url = args.base_url or ("https://api.hcompany.ai/v1" if hosted else "http://127.0.0.1:1234/v1")
    args.model = args.model or ("holo4-27b" if hosted else "holo-3.1-4b")
    if getattr(args, "thinking", None) is None:
        args.thinking = hosted
    if getattr(args, "max_tokens", None) is None:
        args.max_tokens = 8192 if hosted else 2048
    inference_headers(args.base_url)  # Fail before changing the VM when a key is missing.


def tool(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties,
                       "required": required, "additionalProperties": False}}}


TOOLS = [
    tool("click", "Click a UI element. Coordinates are normalized to 0–1000.", {
        "element": {"type": "string"},
        "x": {"type": "integer", "minimum": 0, "maximum": 1000},
        "y": {"type": "integer", "minimum": 0, "maximum": 1000},
        "button": {"type": "string", "enum": ["left", "right"]},
        "clicks": {"type": "integer", "minimum": 1, "maximum": 2},
    }, ["element", "x", "y"]),
    tool("type_text", "Type text into the focused field (ASCII supported).", {
        "text": {"type": "string"}}, ["text"]),
    tool("press_key", "Press a key or simultaneous shortcut, e.g. ['ctrl','s'] or ['enter']. "
         "Use PyAutoGUI key names: ctrl, alt, shift, win, enter, esc, tab, backspace.", {
        "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1}}, ["keys"]),
    tool("scroll", "Scroll vertically; positive clicks scroll up, negative down.", {
        "clicks": {"type": "integer", "minimum": -20, "maximum": 20}}, ["clicks"]),
    tool("wait", "Wait briefly for the desktop to update.", {}, []),
    tool("answer", "Finish the task and provide a final answer.", {
        "content": {"type": "string"}}, ["content"]),
]

SYSTEM = ("You are a computer-use agent operating an Ubuntu desktop. "
          "Use the provided tools to complete the user's task. "
          "Coordinates are integers normalized to [0,1000] relative to the screenshot. "
          "Observe the results of your actions and call answer when finished.")


def action_code(name, args, size):
    if name == "wait":
        return "import time; time.sleep(2)"
    if name == "click":
        x, y = args["x"], args["y"]
        if not (0 <= x <= 1000 and 0 <= y <= 1000):
            raise ValueError("Coordinates outside [0,1000]")
        x, y = min(size[0] - 1, int(x * size[0] / 1000)), min(size[1] - 1, int(y * size[1] / 1000))
        button, clicks = args.get("button", "left"), args.get("clicks", 1)
        if button not in ("left", "right") or clicks not in (1, 2):
            raise ValueError("Invalid click parameters")
        code = f"pyautogui.click({x}, {y}, clicks={clicks}, interval=0.15, button={button!r})"
    elif name == "type_text":
        text = args["text"]
        if not text.isascii():
            raise ValueError("This smoke-test typing tool supports ASCII only")
        code = f"pyautogui.write({text!r}, interval=0.02)"
    elif name == "press_key":
        keys = args["keys"]
        if not keys or not all(isinstance(key, str) for key in keys):
            raise ValueError("Invalid keys")
        code = f"keys={keys!r}; assert all(k in pyautogui.KEYBOARD_KEYS for k in keys); pyautogui.hotkey(*keys)"
    elif name == "scroll":
        clicks = args["clicks"]
        if not isinstance(clicks, int) or not -20 <= clicks <= 20:
            raise ValueError("Invalid scroll amount")
        code = f"pyautogui.scroll({clicks})"
    else:
        raise ValueError(f"Unknown tool: {name}")
    return code


def execute(vm_url, name, args, size):
    code = action_code(name, args, size)
    # Generated code executes only in the VM, never in the host Python process.
    response = requests.post(vm_url + "/execute", json={
        "command": ["python", "-c", "import pyautogui; " + code], "shell": False}, timeout=30)
    response.raise_for_status()
    result = response.json()
    if result.get("returncode", 0) != 0:
        raise RuntimeError(str(result))
    time.sleep(0.8)
    return result


def run(args, output):
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": args.task}]
    for step in range(1, args.max_steps + 1):
        screenshot = requests.get(args.vm_url + "/screenshot", timeout=30)
        screenshot.raise_for_status()
        raw = screenshot.content
        size = Image.open(io.BytesIO(raw)).size
        (output / f"step-{step:02d}.png").write_bytes(raw)
        messages.append({"role": "user", "content": [
            {"type": "text", "text": "<observation>\n"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(raw).decode()}},
            {"type": "text", "text": "\n</observation>"}]})
        seen = 0
        for message in reversed(messages):
            if not isinstance(message.get("content"), list):
                continue
            for chunk in message["content"]:
                if chunk.get("type") == "image_url":
                    seen += 1
                    if seen > args.history_images:
                        chunk.clear()
                        chunk.update(type="text", text="[screenshot evicted]")
        payload = {"model": args.model, "messages": messages, "tools": TOOLS,
                   "tool_choice": "required", "temperature": 0.8,
                   "max_tokens": args.max_tokens,
                   "chat_template_kwargs": {"enable_thinking": args.thinking}}
        print(f"Step {step}: requesting {args.model}, screenshot {size}", flush=True)
        started = time.monotonic()
        response = requests.post(args.base_url.rstrip("/") + "/chat/completions",
                                 json=payload, headers=inference_headers(args.base_url), timeout=180,
                                 allow_redirects=False)
        (output / f"step-{step:02d}-response.json").write_text(response.text)
        response.raise_for_status()
        body = response.json()
        choice = body["choices"][0]
        message = choice["message"]
        calls = message.get("tool_calls") or []
        messages.append({"role": "assistant", "content": message.get("content"),
                         **({"tool_calls": calls} if calls else {})})
        print(f"  {time.monotonic() - started:.1f}s; finish={choice.get('finish_reason')}", flush=True)
        if not calls:
            raise RuntimeError(f"No parsed tool calls returned: {message}")
        for call in calls:
            name = call["function"]["name"]
            values = json.loads(call["function"]["arguments"])
            print(f"  {name}: {values}", flush=True)
            if name == "answer":
                return {"status": "agent_finished", "steps": step, "answer": values["content"]}
            result = execute(args.vm_url, name, values, size)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
            with (output / "actions.jsonl").open("a") as log:
                log.write(json.dumps({"step": step, "tool": name, "arguments": values, "result": result}) + "\n")
    return {"status": "step_limit", "steps": args.max_steps}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["lmstudio", "hcompany"], default="lmstudio")
    parser.add_argument("--env-file", help="File containing HAI_API_KEY or H_API_KEY; never executed as shell code")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--vm-url", default="http://172.16.136.128:5000")
    parser.add_argument("--task", required=True)
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--max-tokens", type=int)
    parser.add_argument("--thinking", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--history-images", type=int, default=3)
    parser.add_argument("--output", type=Path, default=Path("results/holo-local") / datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    configure_inference(args)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "config.json").write_text(json.dumps({**vars(args), "output": str(args.output), "system_prompt": SYSTEM, "tools": TOOLS, "temperature": 0.8, "enable_thinking": args.thinking}, indent=2))
    print(f"Artifacts: {args.output.resolve()}", flush=True)
    try:
        summary = run(args, args.output)
    except (Exception, KeyboardInterrupt) as exc:
        summary = {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "error", "error": str(exc)}
        raise
    finally:
        (args.output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
