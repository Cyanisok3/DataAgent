"""模型动作契约；DSML 仅作输入适配，仍走统一工具校验与执行边界。"""

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from tools import TOOLS


class ToolAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    thought: str
    tool: str
    args: dict[str, object]


class AnswerAction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    thought: str
    evidence_ids: list[str]
    final_query_id: str | None = None
    mode: Literal["answer", "clarify"] = "answer"


ACTION_SCHEMA: TypeAdapter[ToolAction | AnswerAction] = TypeAdapter(ToolAction | AnswerAction)
_MARKER = "<｜｜DSML｜｜"
_INVOKE = re.compile(r'<｜｜DSML｜｜ invoke name="(\w+)">')
_PARAMETER = re.compile(
    r'<｜｜DSML｜｜ parameter name="(\w+)" string="(true|false)">'
    r'(.*?)</｜｜DSML｜｜ parameter>', re.DOTALL,
)


def _dsml_action(content: str) -> dict:
    """每次只采用首个完整 invoke；不跳过非法调用去执行后续动作。"""
    opening = _INVOKE.search(content)
    if opening is None or opening[1] not in {*TOOLS, "ToolAction"}:
        raise ValueError("unknown_or_missing_tool")
    end = content.find("</｜｜DSML｜｜ invoke>", opening.end())
    if end < 0:
        raise ValueError("incomplete_invoke")
    body = content[opening.end():end].strip()
    params: dict[str, object] = {}
    while body:
        match = _PARAMETER.match(body)
        if match is None or match[1] in params or _MARKER in match[3]:
            raise ValueError("invalid_or_duplicate_parameter")
        params[match[1]] = match[3] if match[2] == "true" else json.loads(match[3])
        body = body[match.end():].strip()
    name = opening[1]
    if name == "ToolAction":
        action = ToolAction.model_validate(params)
        if action.tool not in TOOLS:
            raise ValueError("unknown_tool")
        return action.model_dump()
    if params.pop("tool", name) != name:
        raise ValueError("conflicting_tool")
    thought = params.pop("thought", f"调用工具 {name}")
    if "args" in params:
        args = params.pop("args")
        if params:
            raise ValueError("mixed_argument_formats")
    else:
        args = params
    return ACTION_SCHEMA.validate_python(
        {"thought": thought, "tool": name, "args": args}
    ).model_dump()


def parse_action(content: str) -> dict:
    """取首个合法 JSON 动作或首个 DSML 调用，不解释其中模拟的工具结果。"""
    # DSML 参数里的 JSON 不是独立动作，不能抢在外层调用前被执行。
    prefix, marker, _ = content.partition(_MARKER)
    decoder = json.JSONDecoder()
    position = 0
    while (position := prefix.find("{", position)) >= 0:
        try:
            value, end = decoder.raw_decode(prefix, position)
        except json.JSONDecodeError:
            position += 1
            continue
        try:
            return ACTION_SCHEMA.validate_python(value).model_dump()
        except ValidationError:
            position = end
    if marker:
        try:
            return _dsml_action(content[len(prefix):])
        except ValueError:
            pass
    raise json.JSONDecodeError("no_valid_action", content, 0)
