"""Convert between Anthropic Messages and the proxy's OpenAI message format."""

import json
import re
import uuid


def _text_content(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise ValueError("Expected text or content blocks")
    texts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise ValueError("Unsupported text content block")
        texts.append(block["text"])
    return "\n".join(texts)


def _gemini_system_content(content):
    _text_content(content)
    blocks = content if isinstance(content, list) else [{"text": content}]
    texts = []
    for block in blocks:
        text = block["text"]
        first_line, separator, rest = text.partition("\n")
        if (first_line == "You are a Claude agent, built on Anthropic's Claude Agent SDK."
                or re.fullmatch(r"x-anthropic-billing-header: cc_version=[\w.]+; cc_entrypoint=[\w-]+;", first_line)):
            text = rest if separator else ""
        if text:
            texts.append(text)
    return "\n".join(texts)


def anthropic_request_to_openai(body: dict, *, gemini_target: bool = False) -> dict:
    if not isinstance(body, dict) or not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("messages must be a nonempty array")
    result = {"model": body.get("model"), "messages": []}
    system = body.get("system")
    if system is not None:
        content = _gemini_system_content(system) if gemini_target else _text_content(system)
        if content:
            result["messages"].append({"role": "system", "content": content})

    tool_names = {}
    for message in body["messages"]:
        if not isinstance(message, dict) or message.get("role") not in ("system", "user", "assistant"):
            raise ValueError("Unsupported message role")
        role = message["role"]
        content = message.get("content", "")
        if role == "system":
            text = _gemini_system_content(content) if gemini_target else _text_content(content)
            if text:
                result["messages"].append({"role": "system", "content": text})
            continue
        if isinstance(content, str):
            result["messages"].append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            raise ValueError("Message content must be text or blocks")

        texts = []
        tool_calls = []
        ordered_user = []
        for block in content:
            if not isinstance(block, dict):
                raise ValueError("Invalid content block")
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            elif kind in ("thinking", "redacted_thinking") and role == "assistant":
                continue
            elif kind == "tool_use" and role == "assistant":
                call_id, name, arguments = block.get("id"), block.get("name"), block.get("input")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name or not isinstance(arguments, dict):
                    raise ValueError("Invalid tool_use block")
                if call_id in tool_names:
                    raise ValueError("Duplicate tool_use id")
                tool_names[call_id] = name
                tool_calls.append({"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                }})
            elif kind == "tool_result" and role == "user":
                call_id = block.get("tool_use_id")
                if not isinstance(call_id, str) or call_id not in tool_names:
                    raise ValueError("tool_result references unknown tool_use id")
                if texts:
                    ordered_user.append({"role": "user", "content": "\n".join(texts)})
                    texts = []
                ordered_user.append({
                    "role": "tool", "tool_call_id": call_id, "name": tool_names[call_id],
                    "content": _text_content(block.get("content", "")),
                })
            else:
                raise ValueError(f"Unsupported {role} content block: {kind}")

        if role == "user":
            if texts:
                ordered_user.append({"role": "user", "content": "\n".join(texts)})
            result["messages"].extend(ordered_user or [{"role": "user", "content": None}])
        else:
            converted = {"role": role, "content": "\n".join(texts) if texts else None}
            if tool_calls:
                converted["tool_calls"] = tool_calls
            result["messages"].append(converted)

    if "max_tokens" in body:
        result["max_tokens"] = body["max_tokens"]
    if "stop_sequences" in body:
        result["stop"] = body["stop_sequences"]
    for field in ("temperature", "top_p"):
        if field in body:
            result[field] = body[field]
    if "tools" in body:
        if not isinstance(body["tools"], list):
            raise ValueError("tools must be an array")
        result["tools"] = []
        for tool in body["tools"]:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("input_schema"), dict):
                raise ValueError("Invalid tool definition")
            result["tools"].append({"type": "function", "function": {
                "name": tool["name"], "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            }})
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("disable_parallel_tool_use"):
            raise ValueError("disable_parallel_tool_use is not supported by this proxy")
        choice_type = choice.get("type")
        if choice_type == "any":
            result["tool_choice"] = "required"
        elif choice_type in ("auto", "none"):
            result["tool_choice"] = choice_type
        elif choice_type == "tool" and isinstance(choice.get("name"), str):
            result["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        else:
            raise ValueError("Unsupported tool_choice")
    elif choice is not None:
        raise ValueError("Unsupported tool_choice")
    return result


def openai_response_to_anthropic(response: dict, model: str) -> dict:
    choices = response.get("choices") or []
    if not choices:
        raise ValueError("Upstream returned no choices")
    choice = choices[0]
    message = choice.get("message") or {}
    blocks = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            arguments = json.loads(fn.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("Invalid tool call arguments") from exc
        if not isinstance(arguments, dict) or not fn.get("name"):
            raise ValueError("Invalid tool call")
        blocks.append({"type": "tool_use", "id": call.get("id") or f"call_{uuid.uuid4().hex}",
                       "name": fn["name"], "input": arguments})
    if not blocks:
        raise ValueError("Upstream returned empty response")
    reason = choice.get("finish_reason")
    stop_reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(reason)
    if stop_reason is None:
        raise ValueError(f"Unsupported upstream finish reason: {reason}")
    usage = response.get("usage") or {}
    return {
        "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "model": model,
        "content": blocks, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                  "output_tokens": usage.get("completion_tokens", 0)},
    }


def gemini_stream_to_anthropic(events, model: str):
    """Translate unwrapped Gemini stream chunks into Anthropic SSE events."""
    message = {
        "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant", "model": model,
        "content": [], "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
    yield "message_start", {"type": "message_start", "message": message}
    index = -1
    active_text = False
    previous_text = {}
    tool_calls = {}
    usage = {"input_tokens": 0, "output_tokens": 0}
    finish_reason = None
    has_content = False

    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Invalid upstream stream event")
        if event.get("__done__"):
            break
        if event.get("error"):
            raise ValueError(f"Upstream stream error: {event['error']}")
        response = event.get("response", event)
        if isinstance(response, dict) and response.get("error"):
            raise ValueError(f"Upstream stream error: {response['error']}")
        if not isinstance(response, dict):
            raise ValueError("Invalid upstream response")
        metadata = response.get("usageMetadata") or {}
        if metadata:
            usage = {
                "input_tokens": metadata.get("promptTokenCount", 0) or 0,
                "output_tokens": metadata.get("candidatesTokenCount", 0) or metadata.get("completionTokenCount", 0) or 0,
            }
        candidates = response.get("candidates") or []
        if not candidates:
            if response.get("promptFeedback", {}).get("blockReason"):
                raise ValueError("Upstream blocked the request")
            continue
        candidate = candidates[0]
        reason = candidate.get("finishReason")
        if reason:
            if reason == "MAX_TOKENS":
                finish_reason = "max_tokens"
            elif reason != "STOP":
                raise ValueError(f"Upstream stopped with {reason}")
            else:
                finish_reason = "end_turn"
        parts = (candidate.get("content") or {}).get("parts") or []
        for position, part in enumerate(parts):
            if not isinstance(part, dict):
                raise ValueError("Invalid upstream content part")
            text = part.get("text") if not part.get("thought") else None
            if isinstance(text, str) and text:
                if not active_text:
                    index += 1
                    active_text = True
                    yield "content_block_start", {"type": "content_block_start", "index": index,
                                                  "content_block": {"type": "text", "text": ""}}
                prior = previous_text.get(position, "")
                delta = text[len(prior):] if prior and text != prior and text.startswith(prior) else text
                if delta:
                    has_content = True
                    yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                                  "delta": {"type": "text_delta", "text": delta}}
                    previous_text[position] = text if prior and text != prior and text.startswith(prior) else prior + text
            if "functionCall" in part:
                call = part["functionCall"] or {}
                name, args = call.get("name"), call.get("args", {})
                if not name or not isinstance(args, dict):
                    raise ValueError("Invalid upstream functionCall")
                signature = part.get("thoughtSignature") or part.get("thought_signature") or ""
                call_id = call.get("id") or ""
                key = (call_id, signature) if call_id or signature else (name, position)
                if key not in tool_calls:
                    encoded_id = f"call_{call_id}|{signature}" if call_id or signature else f"call_{uuid.uuid4().hex}"
                    tool_calls[key] = (encoded_id, name, args)
                else:
                    encoded_id, _, _ = tool_calls[key]
                    tool_calls[key] = (encoded_id, name, args)
                has_content = True

    if active_text:
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    for encoded_id, name, args in tool_calls.values():
        index += 1
        yield "content_block_start", {"type": "content_block_start", "index": index,
                                      "content_block": {"type": "tool_use", "id": encoded_id,
                                                        "name": name, "input": {}}}
        yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                      "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}}
        yield "content_block_stop", {"type": "content_block_stop", "index": index}
    if not has_content:
        raise ValueError("Upstream returned no content")
    if finish_reason is None:
        raise ValueError("Upstream stream ended without a finish reason")
    if tool_calls and finish_reason != "max_tokens":
        finish_reason = "tool_use"
    yield "message_delta", {"type": "message_delta", "delta": {
        "stop_reason": finish_reason or "end_turn", "stop_sequence": None,
    }, "usage": usage}
    yield "message_stop", {"type": "message_stop"}
