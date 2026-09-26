"use client";

import { useState, useRef, useEffect } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

type Trace = { type: "thinking" | "tool_call" | "tool_result", content: string };
type Turn = { id: number, user: string, traces: Trace[], answer: string,
    status: "running" | "completed" | "failed" | "cancelled", error?: string };
type Usage = {
    session_id: string;
    projected_tokens?: number;
    serialized_bytes?: number;
    last_llm_call?: { usage: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null };
    compressed: boolean;
};

export default function ChatPage() {
    const [turns, setTurns] = useState<Turn[]>([]);
    const [input, setInput] = useState("");
    const [loading, setLoading] = useState(false);
    const [usage, setUsage] = useState<Usage | null>(null);
    const turnIdRef = useRef(0);  // 轮次自增 id
    const sessionIdRef = useRef("");
    const scrollRef = useRef<HTMLDivElement>(null);  // 对话滚动窗口

    // 自动滚到底部：新消息/新 chunk 到达时
    useEffect(() => {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    }, [turns]);

    async function loadUsage() {
        try {
            const res = await fetch("http://localhost:8000/usage?session_id=" + encodeURIComponent(sessionIdRef.current));
            setUsage(await res.json());
        } catch (err) {
            console.error(err);
        }
    }

    async function handleSend() {
        if (!input.trim() || loading) return;
        sessionIdRef.current ||= crypto.randomUUID();
        setLoading(true);

        // 新建一轮：用户问题 + 空轨迹 + 空回答
        const turnId = ++turnIdRef.current;
        setTurns(prev => [...prev, { id: turnId, user: input, traces: [], answer: "", status: "running" }]);
        setInput("");

        try {
            const res = await fetch("http://localhost:8000/chat/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: input, session_id: sessionIdRef.current })
            });
            if (!res.ok) throw new Error(`请求失败：HTTP ${res.status}`);

            const reader = res.body?.getReader();
            if (!reader) throw new Error("响应没有流式 body");
            const decoder = new TextDecoder();
            let buffer = "";
            let receivedDone = false;

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
                            content: (event.is_error ? "查询失败：" : "") + event.output
                        });
                    } else if (event.type === "text_chunk" || event.type === "text") {
                        // 追加到当前轮的 answer（逐字）
                        setTurns(prev => prev.map(t =>
                            t.id === turnId ? { ...t, answer: t.answer + event.content } : t
                        ));
                    } else if (event.type === "error") {
                        setTurns(prev => prev.map(t => t.id === turnId ? { ...t, error: event.content ?? event.error } : t));
                    } else if (event.type === "done") {
                        receivedDone = true;
                        setTurns(prev => prev.map(t => t.id === turnId ? { ...t, status: event.status, error: event.error } : t));
                    }
                }
            }
            if (!receivedDone) throw new Error("连接已结束，但未收到完成事件；当前回答可能不完整。");
        } catch (err) {
            setTurns(prev => prev.map(t => t.id === turnId ? {
                ...t, status: "failed", error: err instanceof Error ? err.message : "请求失败"
            } : t));
        } finally {
            setLoading(false);
            loadUsage();  // 每轮结束刷新水位条
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
            <p className="text-sm text-gray-500 mb-4">
                这是一个基于 DataAgent 的聊天界面，可以与 AI 模型进行交互。
            </p>

            {/* 上下文水位条：投影用量构成 + 预算（L20/L22 真实口径） */}
            {usage && (
                <div className="mb-4 text-xs text-gray-500">
                    <div className="flex justify-between mb-1">
                        <span>最近输入估算 {usage.projected_tokens ?? "未知"} tokens · {usage.serialized_bytes ?? "未知"} bytes</span>
                        <span>实际输入 {usage.last_llm_call?.usage?.prompt_tokens ?? "未提供"} · 输出 {usage.last_llm_call?.usage?.completion_tokens ?? "未提供"}{usage.compressed ? " · 已压缩" : ""}</span>
                    </div>
                </div>
            )}

            {/* 对话流：固定高度滚动窗口，不让消息无限往下堆 */}
            <div ref={scrollRef} className="h-[70vh] overflow-y-auto space-y-6 mb-4 pr-2">
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
                                {tr.type === "tool_result" ?
                                    <details><summary>工具结果（展开全文）</summary><pre className="whitespace-pre-wrap break-all">{tr.content}</pre></details> :
                                    (tr.type === "thinking" ? "行动说明：" : "工具调用：") + tr.content}
                            </div>
                        ))}
                        <p className="text-xs text-gray-500">
                            {t.status === "running" ? "处理中" : t.status === "completed" ? "已完成" : "未完成（保留部分回答）"}
                            {t.error && " · " + t.error}
                        </p>

                        {/* 本轮回答：markdown 渲染（表格/列表/加粗） */}
                        {t.answer && (
                            <div>
                                <div className="inline-block p-3 rounded max-w-[80%] bg-gray-100 text-gray-900">
                                    <ReactMarkdown
                                        remarkPlugins={[remarkGfm]}
                                        components={{
                                            table: (props) => (
                                                <table className="border-collapse my-1 text-sm" {...props} />
                                            ),
                                            th: (props) => (
                                                <th className="border px-2 py-1 bg-gray-200 font-semibold" {...props} />
                                            ),
                                            td: (props) => (
                                                <td className="border px-2 py-1" {...props} />
                                            ),
                                            p: (props) => <p className="my-1" {...props} />,
                                            ul: (props) => <ul className="list-disc pl-5 my-1" {...props} />,
                                            ol: (props) => <ol className="list-decimal pl-5 my-1" {...props} />,
                                            strong: (props) => <strong className="font-semibold" {...props} />,
                                            code: (props) => (
                                                <code className="bg-gray-200 px-1 rounded text-sm" {...props} />
                                            ),
                                            pre: (props) => (
                                                <pre className="bg-gray-800 text-white p-2 rounded overflow-x-auto text-sm my-1" {...props} />
                                            ),
                                        }}
                                    >
                                        {t.answer}
                                    </ReactMarkdown>
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
