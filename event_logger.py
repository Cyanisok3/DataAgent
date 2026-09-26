"""
event_logger.py —— 事件→落库映射（L29 从 main.py 抽出）

职责：把 ReAct 事件流中的 tool_result 事件翻译成 sessions.db 的一行记录。
main.py 只负责 HTTP 层和事件流编排，落库细节在这里。

依赖方向：event_logger → session_store + context（无反向依赖）。
"""
import json

from context import CONTEXT_TOOLS, KIND_CONTEXT, KIND_ERROR, KIND_RESULT
from session_store import save_message


def log_tool_result(event: dict, session_id: str, turn: int) -> None:
    """
    把一个 tool_result 事件落库到 messages 表。

    kind 判定：
      执行失败       → KIND_ERROR（留档审计，不占历史事实名额）
      schema/口径/表清单 → KIND_CONTEXT（引导性，消费完即弃）
      execute_sql 成功 → KIND_RESULT（事实性，可被追问引用 / read_result）

    header：工具名+参数；对 execute_sql 附加护栏改写后实际执行的 SQL，
    区分"模型提交的 SQL"与"实际执行的 SQL"。
    full_output：完整查询结果（L29 工具结果完整留档），优先于 output 落库，
    保证 read_result 能拿到完整数据而非前 20 行截断版。
    """
    if event.get("is_error"):
        kind = KIND_ERROR
    elif event["name"] in CONTEXT_TOOLS:
        kind = KIND_CONTEXT
    else:
        kind = KIND_RESULT

    header = f"[{event['name']}({json.dumps(event.get('input', {}), ensure_ascii=False)})"
    if event.get("sql"):
        header += f" → SQL: {event['sql']}"
    header += "]"

    # L29：优先落完整结果（full_output），保证 read_result 有效；
    # 没有 full_output 时退回 output（前 20 行展示版）
    body = event.get("full_output") or event["output"]
    save_message(session_id, "tool", f"{header}\n{body}", kind, turn=turn)


def log_invocation(event: dict) -> None:
    """控制台可观测性日志：工具调用编号+耗时+工具名+实际SQL。"""
    print(f"[工具#{event.get('invocation', '?')} "
          f"{event['name']} {event.get('elapsed_ms', '?')}ms"
          f"{' ERROR' if event.get('is_error') else ''}] "
          f"{event.get('sql') or event.get('input', '')}")
