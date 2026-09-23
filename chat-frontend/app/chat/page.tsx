"use client";

import { useState, useRef } from "react";

type Trace = { type: "thinking" | "tool_call" | "tool_result", content: string };
type Turn = { id: number, user: string, traces: Trace[], answer: string };

export default function ChatPage() {
    const [turns, setTurns] = useState<Turn[]>([]);
    const [input, setInput] = useState("");
    const [loading, setLoading] = useState(false);
    const turnIdRef = useRef(0);  // 轮次自增 id

    async function handleSend() {
        if (!input.trim()) return;
        setLoading(true);

        // 新建一轮：用户问题 + 空轨迹 + 空回答
        const turnId = ++turnIdRef.current;
        setTurns(prev => [...prev, { id: turnId, user: input, traces: [], answer: "" }]);
        setInput("");

        try {
            const res = await fetch("http://localhost:8000/chat/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: input, session_id: "test-1" })
            });

            const reader = res.body?.getReader();
            if (!reader) throw new Error("响应没有流式 body");
            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const frames = buffer.split("\n\n");
                buffer = frames.pop() ?? "";

                for (const frame of frames) {
                    const line = frame.replace("data: ", "");
                    if (!line) continue;
                    const event = JSON.parse(line);

                    // 事件分流：轨迹 → 当前轮的 traces；回答 → 当前轮的 answer
                    if (event.type === "thinking") {
                        appendTrace(turnId, { type: "thinking", content: event.content });
                    } else if (event.type === "tool_call") {
                        appendTrace(turnId, {
                            type: "tool_call",
                            content: `${event.name}(${JSON.stringify(event.input)})`
                        });
                    } else if (event.type === "tool_result") {
                        appendTrace(turnId, {
                            type: "tool_result",
                            content: event.output.slice(0, 200)  // 截断，防止刷爆页面
                        });
                    } else if (event.type === "text_chunk") {
                        // 追加到当前轮的 answer（逐字）
                        setTurns(prev => prev.map(t =>
                            t.id === turnId ? { ...t, answer: t.answer + event.content } : t
                        ));
                    }
                }
            }
        } catch (err) {
            console.error(err);
        } finally {
            setLoading(false);
        }
    }

    // 辅助：给指定轮次的 traces 追加一条
    function appendTrace(turnId: number, trace: Trace) {
        setTurns(prev => prev.map(t =>
            t.id === turnId ? { ...t, traces: [...t.traces, trace] } : t
        ));
    }

    return (
        <div className="max-w-2xl mx-auto p-4">
            <h1 className="text-2xl font-bold mb-4">Data Agent</h1>

            {/* 对话流：每轮 = 用户问题 → 轨迹 → 回答 */}
            <div className="space-y-6 mb-4">
                {turns.map(t => (
                    <div key={t.id} className="space-y-2">
                        {/* 用户问题 */}
                        <div className="text-right">
                            <div className="inline-block p-2 rounded max-w-[80%] bg-blue-500 text-white">
                                {t.user}
                            </div>
                        </div>

                        {/* 本轮轨迹：思考 / 调工具 / 结果（回答之前） */}
                        {t.traces.map((tr, i) => (
                            <div key={i} className={
                                tr.type === "thinking" ? "text-xs text-gray-400 italic" :
                                    tr.type === "tool_call" ? "text-xs border-blue-200 rounded p-1" :
                                        "text-xs border-green-200 rounded p-1"
                            }>
                                {tr.type === "thinking" ? "💭 " + tr.content :
                                    tr.type === "tool_call" ? "🔧 " + tr.content :
                                        "✅ " + tr.content}
                            </div>
                        ))}

                        {/* 本轮回答 */}
                        {t.answer && (
                            <div>
                                <div className="inline-block p-2 rounded max-w-[80%] bg-gray-100 text-gray-900">
                                    {t.answer}
                                </div>
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {/* 输入框 */}
            <div className="flex gap-2">
                <input
                    className="flex-1 border p-2 rounded"
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleSend()}
                    placeholder="问点什么..."
                />
                <button
                    className="bg-blue-500 text-white px-4 py-2 rounded"
                    onClick={handleSend}
                    disabled={loading}
                >
                    {loading ? "..." : "发送"}
                </button>
            </div>
        </div>
    );
}
