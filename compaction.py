"""在会话锁内按需压缩：模型选片段，代码保留原文与来源，不自由改写数字。"""

import hashlib
import json
import re

from pydantic import BaseModel, ConfigDict

from context import estimate_tokens, serialize, visible_history
from llm import WATERMARK_TOKENS, decision_view, summarize_history
from run_context import RunCancelled
from session_store import apply_summary, load_messages, save_event


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    keep: list[int]


def paragraphs_for(segment):
    paragraphs: list[dict] = []
    for m in segment:
        # 用户修正/确认、旧验证摘要作为不可分割的原文；不让摘要猜任务状态。
        parts = (
            [m["content"]]
            if m["role"] == "user" or m.get("is_summary")
            else m["content"].split("\n\n")
        )
        paragraphs.extend(
            {
                "source_id": m["id"],
                "role": m["role"],
                "text": text,
                "mandatory": m["role"] == "user"
                or bool(m.get("is_summary"))
                or bool(re.search(r"\d", text)),
            }
            for text in parts
            if text.strip()
        )
    return paragraphs


def validate_summary(raw: str, paragraphs: list[dict]) -> str:
    keep = Selection.model_validate(json.loads(raw)).keep
    if len(keep) != len(set(keep)) or any(i < 0 or i >= len(paragraphs) for i in keep):
        raise ValueError("invalid_summary_selection")
    required = {i for i, p in enumerate(paragraphs) if p["mandatory"]}
    # 数字与用户原文由代码强制保留，模型无权删除或互换其归属。
    selected = sorted(set(keep) | required)
    summary = "[已结束历史摘录，不是当前待办]\n" + serialize(
        [
            {k: p[k] for k in ("source_id", "role", "text")}
            for i, p in enumerate(paragraphs)
            if i in selected
        ]
    )
    if not selected:
        raise ValueError("empty_summary")
    if estimate_tokens(summary) > WATERMARK_TOKENS:
        raise ValueError("summary_over_budget")
    return summary


def maybe_compress(sid, turn, question, results):
    history = load_messages(sid)
    view = decision_view([{"role": "user", "content": question}], history, results)
    if not view.omitted:
        return
    visible = visible_history(history)
    latest = max((m["turn"] for m in visible), default=0)
    segment = [m for m in visible if m["turn"] < latest and not m.get("protected")]
    if len(segment) < 2:
        return
    source = serialize(segment)
    audit = {
        "type": "compaction",
        "source_ids": [m["id"] for m in segment],
        "source_version": hashlib.sha256(source.encode()).hexdigest(),
        "before_bytes": len(source.encode()),
        "status": "rejected",
        "raw": None,
    }
    try:
        paragraphs = paragraphs_for(segment)
        raw = summarize_history(paragraphs)
        audit["raw"] = raw
        summary = validate_summary(raw, paragraphs)
        before = serialize(
            [{"role": m["role"], "content": m["content"]} for m in segment]
        )
        after = serialize([{"role": "assistant", "content": summary}])
        audit.update(before_bytes=len(before.encode()), after_bytes=len(after.encode()))
        if len(after.encode()) >= len(before.encode()):
            raise ValueError("summary_no_gain")
        source_ids = {m["id"] for m in segment}
        candidate = [m for m in history if m["id"] not in source_ids]
        candidate.append(
            {
                "id": -1,
                "role": "assistant",
                "content": summary,
                "is_summary": 1,
                "source_ids": sorted(source_ids),
                "turn": min(m["turn"] for m in segment),
                "logical_position": min(
                    m.get("logical_position") or m["id"] for m in segment
                ),
            }
        )
        candidate_view = decision_view([{"role": "user", "content": question}], candidate, results)
        audit.update(before_request_tokens=view.estimated_tokens,
                     after_request_tokens=candidate_view.estimated_tokens)
        if candidate_view.estimated_tokens >= view.estimated_tokens:
            raise ValueError("request_no_gain")
        apply_summary(sid, segment, summary)
        audit["status"] = "committed"
    except RunCancelled:
        audit.update(status="cancelled", reason="client_disconnected")
        raise
    except Exception as exc:  # noqa: BLE001 — 一次尝试，退回有记录的整轮省略
        audit["reason"] = str(exc)
    finally:
        save_event(sid, turn, audit)
