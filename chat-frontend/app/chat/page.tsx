"use client";

import { useState } from "react";

export default function ChatPage() {
    const [messages, setMessages] = useState<{ role: string, content: string }[]>([]);
    const [input, setInput] = useState("");
    const [loading, setLoading] = useState(false);

    async function handleSend() {
        if (!input.trim()) return;
        setLoading(true);
        setMessages([...messages, { role: "user", content: input }]);
        setInput("");

        // 先加一条 assistant 空消息，后续往里面追加内容
        setMessages(prev => [...prev, { role: "assistant", content: "" }]);

        try {
            const res = await fetch("http://localhost:8000/chat/stream", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ message: input, session_id: "test-1" })
            });

            const reader = res.body.getReader();
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

                    if (event.type === "text") {
                        // 追加到最后一条 assistant 消息
                        setMessages(prev => {
                            const newMsgs = [...prev];
                            newMsgs[newMsgs.length - 1] = {
                                role: "assistant",
                                content: newMsgs[newMsgs.length - 1].content + event.content
                            };
                            return newMsgs;
                        });
                    }
                }
            }
        } catch (err) {
            console.error(err);
        } finally {
            setLoading(false);
        }
    }

    return (
        <div className="max-w-2xl mx-auto p-4">
            <h1 className="text-2xl font-bold mb-4">Data Agent</h1>

            {/* 消息列表 */}
            <div className="space-y-4 mb-4">
                {messages.map((m, i) => (
                    <div key={i} className={m.role === "user" ? "text-right" : ""}>
                        <div className="inline-block p-2 rounded bg-gray-700">
                            {m.content}
                        </div>
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