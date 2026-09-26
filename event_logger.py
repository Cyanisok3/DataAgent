"""模型调用日志：完整请求与部分响应可重建，usage 缺失明确为 null。"""

import json
from dataclasses import asdict

from context import serialize
from run_context import CURRENT_RUN
from session_store import connection


def start_call(phase, view, config):
    run = CURRENT_RUN.get()
    if not run or not run.session_id:
        return None
    with connection() as conn:
        return conn.execute(
            """INSERT INTO model_calls
            (session_id,turn,phase,request,config,view,status) VALUES (?,?,?,?,?,?,'running')""",
            (
                run.session_id,
                run.turn,
                phase,
                serialize(view.messages),
                serialize(config),
                serialize(asdict(view)),
            ),
        ).lastrowid


def finish_call(call_id, response, usage, status, elapsed_ms, error=None):
    if call_id is None:
        return
    with connection() as conn:
        conn.execute(
            """UPDATE model_calls SET response=?,usage=?,status=?,elapsed_ms=?,error=?
            WHERE id=? AND status='running'""",
            (response, serialize(usage), status, elapsed_ms, error, call_id),
        )


def usage_stats(sid):
    with connection() as conn:
        rows = conn.execute(
            "SELECT * FROM model_calls WHERE session_id=? ORDER BY id", (sid,)
        ).fetchall()
        compressed = conn.execute(
            """SELECT COUNT(*) FROM messages WHERE session_id=?
            AND is_summary=1 AND source_ids IS NOT NULL AND replaced_by IS NULL""",
            (sid,),
        ).fetchone()[0]
    if not rows:
        return {
            "session_id": sid,
            "last_llm_call": None,
            "compressed": bool(compressed),
        }
    latest = dict(rows[-1])
    view = json.loads(latest["view"])
    usage = json.loads(latest["usage"]) if latest["usage"] else None
    round_calls = [dict(r) for r in rows if r["turn"] == latest["turn"]]
    available = [
        json.loads(r["usage"]) for r in round_calls if r["usage"] not in (None, "null")
    ]
    return {
        "session_id": sid,
        "unit": "estimated_tokens",
        "projected_tokens": view["estimated_tokens"],
        "serialized_bytes": view["serialized_bytes"],
        "omitted": view["omitted"],
        "compressed": bool(compressed),
        "last_llm_call": {
            "phase": latest["phase"],
            "model": json.loads(latest["config"])["model"],
            "status": latest["status"],
            "usage": usage,
        },
        "round_usage": {
            k: sum(u.get(k, 0) for u in available) if available else None
            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        },
        "missing_usage_phases": [
            r["phase"] for r in round_calls if r["usage"] in (None, "null")
        ],
    }
