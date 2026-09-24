"""
compaction.py —— 压缩编排（L24 从 main.py 移出的胶水层）

职责：把"什么时候压缩"串成一条完整动作——
  per-session 锁（并发安全）→ 同口径判定（find_compressible）→
  LLM 摘要（summarize_history）→ 落账本（apply_summary）

main.py 只调一行：threading.Thread(target=maybe_compress, ...)。
本文件依赖 session_store / context / llm，不被它们反向依赖（无环）。
"""
import threading

from context import find_compressible
from session_store import load_messages, apply_summary
from llm import summarize_history

# per-session 压缩锁——同会话压缩串行化，跨会话并行。
# 锁字典本身也要一把锁保护（多线程并发 setdefault 可能丢条目）。
_compress_locks: dict[str, threading.Lock] = {}
_compress_locks_guard = threading.Lock()


def maybe_compress(session_id: str):
    """压缩：找装不下的旧段 → LLM 摘要 → 落账本。
    投影按 WATERMARK_TOKENS 预算从新往回装；find_compressible 返回
    投影装不下的旧段——这就是"该压缩什么"的判定（同口径）。
    deepseek-flash 窗口 1M token，正常会话不会走到这里；
    罕见路径也必须正确，异常显式打印、不静默吞掉。"""
    with _compress_locks_guard:
        lock = _compress_locks.setdefault(session_id, threading.Lock())
    with lock:
        try:
            history = load_messages(session_id)
            segment = find_compressible(history)
            if len(segment) < 2:
                return   # 预算装得下全部对话：没有值得压缩的旧段
            summary = summarize_history(segment)
            if summary:
                apply_summary(session_id, segment, summary)
        except Exception as e:
            print(f"[compress] session={session_id} 压缩失败: {e}")
