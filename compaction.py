"""
compaction.py —— 压缩编排（L24 从 main.py 移出的胶水层）

职责：把"什么时候压缩"串成一条完整动作——
  per-session 锁（并发安全）→ 同口径判定（find_compressible）→
  LLM 摘要（summarize_history）→ 保真验证（_verify_summary）→
  落账本（apply_summary）

main.py 只调一行：threading.Thread(target=maybe_compress, ...)。
本文件依赖 session_store / context / llm，不被它们反向依赖（无环）。

L29：摘要保真验证——原始 segment 有关键数字但摘要完全无数字时拒绝提交，
防止压缩后丢失事实（审计文档 P2：摘要非空就替换，没有验证数字保真）。
"""
import re
import threading

from context import find_compressible
from session_store import load_messages, apply_summary
from llm import summarize_history

# per-session 压缩锁——同会话压缩串行化，跨会话并行。
# 锁字典本身也要一把锁保护（多线程并发 setdefault 可能丢条目）。
_compress_locks: dict[str, threading.Lock] = {}
_compress_locks_guard = threading.Lock()


def _verify_summary(summary: str, segment: list[dict]) -> bool:
    """摘要保真验证（L29）：原始 segment 有关键数字但摘要完全无数字时拒绝。

    审计文档问题：摘要非空就替换，可能丢失今年销售额等关键数字。
    验证逻辑：
      1. 提取原始 segment 中的所有数字（含小数）
      2. 提取摘要中的所有数字
      3. 原始有数字但摘要完全无数字 → 拒绝（明显丢了事实）
      4. 数字覆盖率低于 10% → 警告但不拒绝（摘要本来就是压缩）

    返回 True 表示通过验证可以提交，False 表示拒绝。
    """
    original_text = " ".join(m.get("content", "") for m in segment)
    original_numbers = set(re.findall(r'\d+\.?\d*', original_text))
    summary_numbers = set(re.findall(r'\d+\.?\d*', summary))

    # 原始 segment 无数字（纯对话），不验证数字
    if not original_numbers:
        return True

    # 原始有数字但摘要完全无数字 → 拒绝
    if not summary_numbers:
        print(f"[compress] 摘要验证失败：原始有 {len(original_numbers)} 个数字，"
              f"摘要中无数字，拒绝提交")
        return False

    # 数字覆盖率警告（不拒绝）
    coverage = len(original_numbers & summary_numbers) / len(original_numbers)
    if coverage < 0.1:
        print(f"[compress] 摘要警告：数字覆盖率仅 {coverage:.1%}，"
              f"可能丢失关键事实（原始 {len(original_numbers)} 个数字，"
              f"摘要保留 {len(summary_numbers)} 个）")
    return True


def maybe_compress(session_id: str):
    """压缩：找装不下的旧段 → LLM 摘要 → 保真验证 → 落账本。
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
            summary = summarize_history(segment, session_id=session_id)
            if not summary:
                return
            # L29：保真验证——数字完全丢失时拒绝提交
            if not _verify_summary(summary, segment):
                print(f"[compress] session={session_id} 摘要未通过保真验证，跳过压缩")
                return
            apply_summary(session_id, segment, summary)
        except Exception as e:
            print(f"[compress] session={session_id} 压缩失败: {e}")
