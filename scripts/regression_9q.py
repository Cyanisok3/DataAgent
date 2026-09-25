"""
L23 九题回归测试：批量发送，解析 SSE，统计工具调用与结果。
用法：.venv/bin/python scripts/regression_9q.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://localhost:8002"
SID = "regression-9q"

QUESTIONS = [
    "今年各门店的总销售额？按高到低",
    "最近90天各门店销售额和订单量",
    "最近90天哪种商品销量最高？",
    "可以",
    "最近一年每个月各门店销售额明细",
    "布鲁克林哪个月销售额最高？",
    "各门店客单价和活跃客户数",
    "刚才的90天销售额合计和占比",
    "整体总结门店运营特点",
]


def stream_chat(msg: str) -> dict:
    """发送一条消息，返回 {tools, answer, ok, elapsed}"""
    body = json.dumps({"message": msg, "session_id": SID}).encode()
    req = urllib.request.Request(
        f"{BASE}/chat/stream",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    tools = []
    answer_parts = []
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = ""
        while True:
            chunk = resp.read(4096).decode("utf-8", errors="replace")
            if not chunk:
                break
            buf += chunk
            while "\n\n" in buf:
                frame, buf = buf.split("\n\n", 1)
                for line in frame.splitlines():
                    if line.startswith("data: "):
                        try:
                            ev = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        t = ev.get("type")
                        if t == "tool_call":
                            tools.append({"name": ev.get("name"),
                                          "input": ev.get("input")})
                        elif t == "text_chunk":
                            answer_parts.append(ev.get("content", ""))
    elapsed = time.time() - t0
    answer = "".join(answer_parts).strip()
    return {"tools": tools, "answer": answer, "ok": bool(answer),
            "elapsed": round(elapsed, 1)}


def main():
    results = []
    for i, q in enumerate(QUESTIONS, 1):
        print(f"\n{'='*60}\nQ{i}: {q}\n{'='*60}")
        try:
            r = stream_chat(q)
        except Exception as e:
            r = {"tools": [], "answer": f"ERROR: {e}", "ok": False, "elapsed": 0}
        results.append(r)
        tool_names = [t["name"] for t in r["tools"]]
        print(f"  工具调用 ({len(tool_names)}): {tool_names}")
        print(f"  回答 ({len(r['answer'])} 字, {r['elapsed']}s): "
              f"{r['answer'][:150]}...")
        if not r["ok"]:
            print("  ⚠️ 无最终回答")

    # 汇总
    print("\n" + "=" * 60)
    print("回归汇总")
    print("=" * 60)
    print(f"{'Q':<3}{'工具数':<6}{'成功':<6}{'工具链'}")
    for i, r in enumerate(results, 1):
        chain = "→".join(t["name"] for t in r["tools"])
        print(f"{i:<3}{len(r['tools']):<6}{'✅' if r['ok'] else '❌':<6}{chain}")

    ok_count = sum(1 for r in results if r["ok"])
    total_tools = sum(len(r["tools"]) for r in results)
    print(f"\n成功: {ok_count}/9 | 总工具调用: {total_tools}")

    # 保存完整结果
    with open("/tmp/regression_9q_result.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("\n完整结果已保存到 /tmp/regression_9q_result.json")


if __name__ == "__main__":
    main()
