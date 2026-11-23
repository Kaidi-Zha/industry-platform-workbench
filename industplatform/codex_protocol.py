"""Translate single Codex Responses turns for Messages and generateContent."""

import base64
import hashlib
import json
import re
import uuid

from . import provider_api as api


def identifier(prefix):
    return prefix + "_" + uuid.uuid4().hex


def tool_alias(name, namespace, used):
    qualified = f"{namespace}__{name}" if namespace else name
    alias = re.sub(r"[^a-zA-Z0-9_-]", "_", qualified)
    if len(alias) > 64 or alias in used or not re.match(r"[a-zA-Z_]", alias):
        alias = "tool_" + hashlib.sha256(qualified.encode()).hexdigest()[:48]
    return alias


def flatten_response_tools(payload):
    """Convert Codex namespaces for Responses providers accepting flat tools."""
    tools, aliases = [], {}
    for tool in payload.get("tools", []):
        if tool["type"] == "namespace":
            for member in tool["tools"]:
                name = tool_alias(member["name"], tool["name"], aliases)
                aliases[name] = (tool["name"], member["name"])
                tools.append({**member, "name": name})
        else:
            tools.append(tool)
    return tools, aliases


def unflatten_item(item, aliases):
    alias = aliases.get(item.get("name"))
    if alias and item.get("type") in {"function_call", "custom_tool_call"}:
        namespace, name = alias
        return {**item, "namespace": namespace, "name": name}
    return item


def deepseek_input(items):
    """Deliver Codex's native inter-agent text through DeepSeek Responses."""
    if not isinstance(items, list):
        return items
    output = []
    for item in items:
        if item.get("type") != "agent_message":
            output.append(item)
            continue
        # The native agent_message envelope carries its task body in the
        # field named encrypted_content. Reasoning items are passed through above.
        fields = {"input_text": "text", "encrypted_content": "encrypted_content"}
        text = "\n".join(part[fields[part["type"]]] for part in item["content"] if part.get("type") in fields)
        if not text.strip():
            raise api.ProviderError("DeepSeek 无法读取此 Codex 委派消息，因为消息没有可读文本")
        author, recipient = item["author"], item["recipient"]
        output.append({"type": "message", "role": "user", "content": [{
            "type": "input_text", "text": f"[Codex agent message from {author} to {recipient}]\n{text}",
        }]})
    return output


def _items(value):
    return [{"type": "message", "role": "user", "content": value}] if isinstance(value, str) else api._blocks(value)


def _parts(value):
    return [{"type": "input_text", "text": value}] if isinstance(value, str) else api._blocks(value)


def _text(value):
    return "\n".join(part["text"] for part in _parts(value) if part.get("type") in {"input_text", "output_text", "text"})


def _arguments(item):
    value = json.loads(item["arguments"]) if item["type"] == "function_call" else {"input": item["input"]}
    if not isinstance(value, dict):
        raise ValueError("tool arguments")
    return value


def _image(part, claude):
    url = part["image_url"]
    if claude and url.startswith("https://"):
        return {"type": "image", "source": {"type": "url", "url": url}}
    match = re.fullmatch(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=]+)", url)
    if not match:
        raise api.ProviderError("当前模型适配器需要内联图片")
    media_type, data = match.groups()
    base64.b64decode(data, validate=True)
    if claude:
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"inlineData": {"mimeType": media_type, "data": data}}


def _content(value, claude):
    blocks = []
    for part in _parts(value):
        if part["type"] in {"input_text", "output_text", "text"}:
            if part["text"]:
                blocks.append({"type": "text", "text": part["text"]} if claude else {"text": part["text"]})
        elif part["type"] == "input_image":
            blocks.append(_image(part, claude))
        else:
            raise api.ProviderError("当前模型适配器不支持该输入类型")
    return blocks


def _tool_result(item, target, claude):
    output = item["output"]
    if claude:
        original_id = target["id"] if target else item["call_id"]
        content = output if isinstance(output, str) else _content(output, True)
        return {"type": "tool_result", "tool_use_id": original_id, "content": content}
    if not isinstance(output, str) and any(part.get("type") == "input_image" for part in output):
        raise api.ProviderError("Gemini 适配器暂不支持含图片的工具结果")
    text = output if isinstance(output, str) else _text(output)
    result = {"name": target["name"], "response": {"output": text}}
    if target.get("id"):
        result["id"] = target["id"]
    return {"functionResponse": result}


class Translation:
    """One Codex process, with provider-native signed blocks retained in memory."""

    def __init__(self, protocol):
        self.protocol = protocol
        self.claude = protocol == api.ANTHROPIC
        self.turns = []
        self.blocks = {}
        self.calls = {}

    def declarations(self, tools):
        declarations, identities = [], {}

        def add(tool, namespace=""):
            kind = tool.get("type")
            if kind == "namespace":
                for child in tool.get("tools", ()):
                    add(child, tool["name"])
                return
            if kind not in {"function", "custom"}:
                return
            name = tool["name"]
            upstream = tool_alias(name, namespace, identities)
            identities[upstream] = (name, namespace, kind)
            schema = tool.get("parameters") or {"type": "object", "properties": {}}
            if kind == "custom":
                schema = {"type": "object", "properties": {"input": {"type": "string"}},
                          "required": ["input"], "additionalProperties": False}
            key = "input_schema" if self.claude else "parametersJsonSchema"
            declaration = {"name": upstream, key: schema}
            if tool.get("description"):
                declaration["description"] = tool["description"]
            declarations.append(declaration)

        for tool in tools:
            add(tool)
        return declarations, identities

    def request(self, payload, model):
        system = [payload.get("instructions") or ""]
        messages, seen = [], set()
        declarations, identities = self.declarations(api._blocks(payload.get("tools", [])))
        names = {value: key for key, value in identities.items()}
        content_key = "content" if self.claude else "parts"

        def append(role, blocks):
            if not blocks:
                return
            if role == "assistant" and not self.claude:
                role = "model"
            if messages and messages[-1]["role"] == role:
                messages[-1][content_key].extend(blocks)
            else:
                messages.append({"role": role, content_key: list(blocks)})

        for item in _items(payload["input"]):
            kind = item.get("type", "message")
            original = self.blocks.get(item.get("call_id") if kind in {"function_call", "custom_tool_call"}
                                       else item.get("id"))
            if original is not None:
                turn, blocks = original
                if turn not in seen:
                    append("assistant", blocks)
                    seen.add(turn)
            elif kind == "message":
                role = item["role"]
                if role in {"system", "developer"}:
                    system.append(_text(item["content"]))
                else:
                    append(role, _content(item["content"], self.claude))
            elif kind in {"function_call", "custom_tool_call"}:
                key = (item["name"], item.get("namespace") or "", "function" if kind == "function_call" else "custom")
                name = names.get(key, item["name"])
                call = {"name": name, "id": item["call_id"]}
                self.calls[item["call_id"]] = call
                args = _arguments(item)
                block = ({"type": "tool_use", **call, "input": args} if self.claude
                         else {"functionCall": {"name": name, "args": args}})
                append("assistant", [block])
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                target = self.calls.get(item["call_id"])
                if target is None:
                    raise api.ProviderError("历史工具输出缺少对应调用")
                append("user", [_tool_result(item, target, self.claude)])

        prompt = "\n\n".join(text for text in system if text)
        max_tokens = payload.get("max_output_tokens") or 8192
        if type(max_tokens) is not int or max_tokens < 1:
            raise ValueError("max_output_tokens")
        max_tokens = min(max_tokens, 32768)
        if self.claude:
            translated = {"model": model, "messages": messages, "max_tokens": max_tokens}
            if prompt:
                translated["system"] = prompt
            if declarations:
                translated["tools"] = declarations
                translated["tool_choice"] = {"type": "auto",
                                             "disable_parallel_tool_use": payload.get("parallel_tool_calls") is not True}
        else:
            translated = {"contents": messages, "generationConfig": {"maxOutputTokens": max_tokens}}
            if prompt:
                translated["systemInstruction"] = {"parts": [{"text": prompt}]}
            if declarations:
                translated["tools"] = [{"functionDeclarations": declarations}]
        schema_format = (payload.get("text") or {}).get("format") or {}
        if schema_format.get("type") == "json_schema":
            schema = schema_format["schema"]
            if self.claude:
                translated["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
            else:
                translated["generationConfig"].update(responseMimeType="application/json", responseJsonSchema=schema)
        return translated, identities

    def response(self, reply, identities, model):
        usage = reply.get("usage", {}) if self.claude else reply.get("usageMetadata", {})
        if self.claude:
            in_tokens = (usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0)
                         + usage.get("cache_read_input_tokens", 0))
            out_tokens = usage.get("output_tokens", 0)
            cached = usage.get("cache_read_input_tokens", 0)
        else:
            in_tokens = usage.get("promptTokenCount", 0)
            out_tokens = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
            cached = usage.get("cachedContentTokenCount", 0)
        assistant, calls, final_text = api._parse_turn(self.protocol, reply)
        blocks = assistant["content" if self.claude else "parts"]
        output = []
        originals = {}
        for upstream, args, original_id in calls:
            if not isinstance(args, dict) or (self.claude and not original_id):
                raise ValueError("tool call")
            name, namespace, kind = identities[upstream]
            call_id = identifier("call")
            originals[call_id] = {"name": upstream, "id": original_id}
            if kind == "custom":
                value = args.get("input")
                if not isinstance(value, str):
                    raise ValueError("custom tool input")
                item = {"type": "custom_tool_call", "id": identifier("ctc"), "input": value}
            else:
                item = {"type": "function_call", "id": identifier("fc"), "arguments": api._json_dump(args)}
            item.update(name=name, call_id=call_id, status="completed")
            if namespace:
                item["namespace"] = namespace
            output.append(item)
        if final_text:
            output.insert(0, message(final_text))
        if not output:
            raise api.ProviderError("模型未返回答复或工具调用")
        anchors = set(originals) | {item["id"] for item in output if item["type"] == "message"}
        turn = identifier("turn")
        self.blocks.update({anchor: (turn, blocks) for anchor in anchors})
        self.calls.update(originals)
        self.turns.append(anchors)
        if len(self.turns) > 128:
            for anchor in self.turns.pop(0):
                self.blocks.pop(anchor, None)
                self.calls.pop(anchor, None)
        return {"id": identifier("resp"), "object": "response", "status": "completed", "model": model,
                "output": output, "usage": {"input_tokens": in_tokens,
                "input_tokens_details": {"cached_tokens": cached}, "output_tokens": out_tokens,
                "output_tokens_details": {"reasoning_tokens": usage.get("thoughtsTokenCount", 0)},
                "total_tokens": in_tokens + out_tokens}}


class ChatTranslation:
    """Translate Codex Responses items to compatible Chat Completions turns."""

    def __init__(self):
        self.assistant_turns = {}

    def request(self, payload, model):
        tools, aliases = flatten_response_tools(payload)
        alias_for = {identity: alias for alias, identity in aliases.items()}
        accepted = {}
        declarations = []
        for original in tools:
            kind = original.get("type")
            if kind not in {"function", "custom"}:
                continue
            name = original["name"]
            schema = original.get("parameters") or {"type": "object", "properties": {}}
            if kind == "custom":
                schema = {"type": "object", "properties": {"input": {"type": "string"}}, "required": ["input"]}
            definition = {"name": name, "parameters": schema}
            if original.get("description"):
                definition["description"] = original["description"]
            declarations.append({"type": "function", "function": definition})
            accepted[name] = kind
        messages = []
        if payload.get("instructions"):
            messages.append({"role": "system", "content": payload["instructions"]})
        for item in _items(payload["input"]):
            kind = item.get("type", "message")
            if kind == "message":
                role = "system" if item["role"] in {"developer", "system"} else item["role"]
                parts = _parts(item["content"])
                if any(part.get("type") == "input_image" for part in parts):
                    content = [{"type": "image_url", "image_url": {"url": part["image_url"]}}
                               if part.get("type") == "input_image"
                               else {"type": "text", "text": part["text"]} for part in parts]
                else:
                    content = _text(item["content"])
                if content:
                    messages.append({"role": role, "content": content})
            elif kind in ("function_call", "custom_tool_call"):
                name = alias_for.get((item.get("namespace"), item["name"]), item["name"])
                call = {"id": item["call_id"], "type": "function",
                        "function": {"name": name, "arguments": api._json_dump(_arguments(item))}}
                if messages and messages[-1]["role"] == "assistant":
                    messages[-1].setdefault("tool_calls", []).append(call)
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif kind in ("function_call_output", "custom_tool_call_output"):
                output = item["output"]
                messages.append({"role": "tool", "tool_call_id": item["call_id"],
                                 "content": output if isinstance(output, str) else api._json_dump(output)})
        for message in messages:
            calls = message.get("tool_calls", [])
            for call in calls:
                saved = self.assistant_turns.get(call["id"])
                if saved:
                    extras, call_fields = saved
                    message.update(extras)
                    call.update(call_fields)
        body = {"model": model, "messages": messages}
        if "max_output_tokens" in payload:
            limit = payload["max_output_tokens"]
            if type(limit) is not int or limit < 1:
                raise ValueError("max_output_tokens")
            body["max_completion_tokens"] = min(limit, 32768)
        if declarations:
            body["tools"] = declarations
            if type(payload.get("parallel_tool_calls")) is bool:
                body["parallel_tool_calls"] = payload["parallel_tool_calls"]
        format_spec = (payload.get("text") or {}).get("format") or {}
        if format_spec.get("type") == "json_schema":
            body["response_format"] = {"type": "json_schema", "json_schema": {
                key: format_spec[key] for key in ("name", "schema", "strict", "description") if key in format_spec
            }}
        return body, (accepted, aliases)

    def response(self, reply, identities, model):
        accepted, aliases = identities
        assistant, calls, text = api._parse_turn(api.CHAT, reply)
        extras = {key: assistant[key] for key in ("reasoning_content", "extra_content") if key in assistant}
        for call in assistant.get("tool_calls", []):
            attributes = {key: call[key] for key in ("extra_content",) if key in call}
            if extras or attributes:
                self.assistant_turns[call["id"]] = (extras, attributes)
        while len(self.assistant_turns) > 512:
            self.assistant_turns.pop(next(iter(self.assistant_turns)))
        api._validate_calls(calls, api.CHAT, accepted)
        output = [message(text)] if text else []
        for name, arguments, call_id in calls:
            if accepted[name] == "custom":
                raw = arguments.get("input")
                if not isinstance(raw, str):
                    raise ValueError("custom tool input")
                item = {"type": "custom_tool_call", "id": identifier("ctc"), "input": raw}
            else:
                item = {"type": "function_call", "id": identifier("fc"), "arguments": api._json_dump(arguments)}
            item.update({"name": name, "call_id": call_id, "status": "completed"})
            output.append(unflatten_item(item, aliases))
        if not output:
            raise api.ProviderError("模型未返回答复或工具调用")
        usage = reply.get("usage") or {}
        in_tokens, out_tokens = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
        return {"id": identifier("resp"), "object": "response", "status": "completed", "model": model,
                "output": output, "usage": {"input_tokens": in_tokens,
                "input_tokens_details": {"cached_tokens": 0}, "output_tokens": out_tokens,
                "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": in_tokens + out_tokens}}


def message(text, phase=None):
    item = {"type": "message", "id": identifier("msg"), "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]}
    if phase:
        item["phase"] = phase
    return item


def events(response):
    """Emit the Responses lifecycle consumed by Codex from a buffered result."""
    yield "response.created", {"response": {**response, "status": "in_progress", "output": []}}
    for index, item in enumerate(response["output"]):
        fields = {"output_index": index}
        start = {**item, "status": "in_progress"}
        delta_type = None
        if item["type"] == "message":
            start["content"] = []
        elif item["type"] in {"function_call", "custom_tool_call"}:
            field = "arguments" if item["type"] == "function_call" else "input"
            start[field] = ""
            delta_type = "response.function_call_arguments.delta" if field == "arguments" else "response.custom_tool_call_input.delta"
        yield "response.output_item.added", {**fields, "item": start}
        if item["type"] == "message":
            for position, part in enumerate(item.get("content", [])):
                if part.get("type") == "output_text":
                    delta = {"item_id": item["id"], **fields, "content_index": position, "logprobs": []}
                    yield "response.output_text.delta", {**delta, "delta": part["text"]}
        elif delta_type:
            yield delta_type, {"item_id": item["id"], **fields, "delta": item[field]}
        yield "response.output_item.done", {**fields, "item": item}
    kind = "response.completed" if response.get("status") == "completed" else "response." + response["status"]
    yield kind, {"response": response}


def sse(events_to_send):
    for seq, (kind, fields) in enumerate(events_to_send):
        data = api._json_dump({"type": kind, "sequence_number": seq, **fields})
        yield ("event: " + kind + "\ndata: " + data + "\n\n").encode()
