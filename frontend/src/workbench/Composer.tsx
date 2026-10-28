import { useRef, useState, type FormEvent } from "react";
import { ArrowUp, RotateCw, LoaderCircle } from "lucide-react";
import { ApiError, errorText } from "../api/client";
export type MessageDraft = { question: string; client_message_id: string };
type Props = {
  onSend: (body: MessageDraft) => Promise<void>;
  disabled?: boolean;
};
export function Composer({ onSend, disabled = false }: Props) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [editableError, setEditableError] = useState(false);
  const pending = useRef<MessageDraft | null>(null);
  async function send(event?: FormEvent) {
    event?.preventDefault();
    if (busy || disabled || (!text.trim() && !pending.current)) return;
    pending.current ??= {
      question: text.trim(),
      client_message_id: crypto.randomUUID(),
    };
    setBusy(true);
    setError("");
    setEditableError(false);
    try {
      await onSend(pending.current);
      pending.current = null;
      setText("");
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) {
        setError(errorText(error));
        if (
          error instanceof ApiError &&
          error.status >= 400 &&
          error.status < 500 &&
          error.status !== 429
        ) {
          pending.current = null;
          setEditableError(true);
        }
      }
    } finally {
      setBusy(false);
    }
  }
  return (
    <form className="composer" onSubmit={(event) => void send(event)}>
      <label htmlFor="analysis-question" className="sr-only">
        分析问题
      </label>
      <textarea
        id="analysis-question"
        aria-label="分析问题"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder="例如：按渠道比较八月和九月的实付销售额，哪些渠道变化较大？"
        rows={3}
        maxLength={4000}
        readOnly={busy || (Boolean(error) && !editableError)}
        disabled={disabled}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            void send();
          }
        }}
      />
      <div className="composer-footer">
        <span>Ctrl / ⌘ + Enter 发送 · 数据不足时会向你确认</span>
        <button
          className="button primary"
          type="submit"
          disabled={disabled || busy || !text.trim()}
        >
          {busy ? (
            <LoaderCircle size={16} className="spin" />
          ) : error ? (
            <RotateCw size={16} />
          ) : (
            <ArrowUp size={16} />
          )}
          {busy
            ? "正在提交"
            : error
              ? editableError
                ? "重新提交"
                : "重试发送"
              : "发送分析"}
        </button>
      </div>
      {error && (
        <p role="alert" className="inline-error">
          {error}
        </p>
      )}
    </form>
  );
}
