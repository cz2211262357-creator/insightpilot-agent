import { LoaderCircle, AlertCircle, RotateCw, Inbox } from "lucide-react";
import type { ReactNode } from "react";
import { errorText } from "../api/client";
export const labels: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "分析中",
  WAITING_INPUT: "待确认",
  SUCCEEDED: "已完成",
  FAILED: "未完成",
  TIMED_OUT: "已超时",
  CANCELLED: "已取消",
  SUPERSEDED: "已被替代",
};
export const metricNames: Record<string, string> = {
  paid_sales: "实付销售额",
  net_sales: "净销售额",
  order_count: "订单量",
  aftersale_rate: "售后申请率",
  refund_amount: "退款金额",
  avg_resolution_hours: "平均处理时长",
  region: "地区",
  region_name: "地区",
  channel: "渠道",
  channel_name: "渠道",
  category: "品类",
  month: "月份",
  period: "统计期间",
  sales: "销售额",
};
export const stages: Record<string, string> = {
  bootstrap: "读取指标目录",
  plan: "制定分析步骤",
  clarification: "等待口径确认",
  generate: "准备只读查询",
  validate: "校验查询范围",
  repair: "修正查询",
  cost_review: "等待查询批准",
  execute: "核验查询结果",
  describe: "生成证据报告",
};
export function StatusPill({ state }: { state: string }) {
  return (
    <span className={"status status-" + state.toLowerCase()}>
      <i />
      {labels[state] ?? state}
    </span>
  );
}
export function Loading({ text = "正在读取" }: { text?: string }) {
  return (
    <div className="loading">
      <LoaderCircle size={20} className="spin" />
      {text}
    </div>
  );
}
export function ErrorNotice({
  error,
  retry,
}: {
  error: unknown;
  retry?: () => void;
}) {
  return (
    <div className="notice error" role="alert">
      <AlertCircle size={18} />
      <span>{errorText(error)}</span>
      {retry && (
        <button onClick={retry}>
          <RotateCw size={14} />
          重试
        </button>
      )}
    </div>
  );
}
export function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      <span className="empty-icon">
        <Inbox size={26} />
      </span>
      <h3>{title}</h3>
      <p>{children}</p>
      {action}
    </div>
  );
}
export function PageTitle({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {action}
    </div>
  );
}
export function dateTime(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}
export function shortId(value: string) {
  return value.slice(0, 8);
}
export function downloadJson(filename: string, value: unknown) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json;charset=utf-8",
    }),
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
