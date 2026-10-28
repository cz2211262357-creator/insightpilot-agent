export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    public requestId?: string,
  ) {
    super(code);
  }
}
const messages: Record<string, string> = {
  authentication_required: "请先登录。",
  identity_expired: "登录已失效，请重新登录。",
  invalid_credentials: "账号或密码不正确。",
  conversation_conflict: "话题已在另一个页面更新，已为你刷新。",
  confirmation_stale: "确认内容已变化，请重新查看任务。",
  confirmation_conflict: "该确认已提交，不能更改决定。",
  confirmation_expired: "确认已过期，请重新发起分析。",
  report_not_ready: "报告尚未完成。",
  dataset_unavailable: "当前数据版本不可访问，报告已隐藏。",
  catalog_changed: "指标目录已更新，请重新发起分析。",
  run_conflict: "任务状态已变化，请刷新后重试。",
  invalid_request: "请检查输入内容。",
  login_rate_limited: "登录尝试过于频繁，请稍后再试。",
  dependency_unavailable: "服务暂时不可用，请稍后重试。",
  model_output_invalid: "模型返回内容未通过校验，请调整问题后重试。",
  sql_repair_exhausted: "查询未能在修正次数内完成，请缩小问题范围。",
  run_budget_exhausted: "本次分析已达到调用上限，请重新发起任务。",
  provider_unavailable: "模型服务暂时不可用，任务将按规则处理。",
  provider_timeout: "模型响应超时，请稍后重试。",
  provider_authorization_denied: "模型服务授权不可用，请联系管理员。",
  provider_request_rejected: "模型服务未接受此次请求，请调整问题后重试。",
  execution_deadline: "本次分析已超时。",
  queue_deadline: "任务排队超时，请重试。",
  authorization_revoked: "权限已发生变化，任务已停止。",
  admin_required: "当前账号没有成员管理权限。",
  csrf_rejected: "页面认证已更新，请刷新后重试。",
};
export function errorText(error: unknown): string {
  if (error instanceof ApiError)
    return messages[error.code] ?? "操作未完成，请刷新状态后重试。";
  return "网络连接中断，可重试本次请求。";
}
let generation = 0;
let expired: () => void = () => {};
const controllers = new Set<AbortController>();
export function sessionGeneration() {
  return generation;
}
export function trackController(controller: AbortController) {
  controllers.add(controller);
  return () => {
    controllers.delete(controller);
  };
}
export function expireSession(at: number) {
  if (at === generation) {
    resetSession();
    expired();
  }
}
export function resetSession() {
  generation += 1;
  for (const c of controllers) c.abort();
  controllers.clear();
}
export function onExpired(callback: () => void) {
  expired = callback;
  return () => {
    expired = () => {};
  };
}
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const token = decodeURIComponent(
    document.cookie
      .split("; ")
      .find((v) => v.startsWith("ip_csrf="))
      ?.slice(8) ?? "",
  );
  const controller = new AbortController();
  controllers.add(controller);
  const at = generation;
  try {
    const response = await fetch("/api/v1" + path, {
      ...options,
      credentials: "same-origin",
      signal: controller.signal,
      headers: {
        ...(options.body ? { "content-type": "application/json" } : {}),
        ...(token ? { "x-csrf-token": token } : {}),
        ...options.headers,
      },
    });
    if (at !== generation)
      throw new DOMException("Stale session", "AbortError");
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      if (at !== generation)
        throw new DOMException("Stale session", "AbortError");
      const error = new ApiError(
        response.status,
        body.error?.code ?? "request_failed",
        body.error?.request_id,
      );
      if (response.status === 401) expireSession(at);
      throw error;
    }
    const result =
      response.status === 204
        ? (undefined as T)
        : ((await response.json()) as T);
    if (at !== generation)
      throw new DOMException("Stale session", "AbortError");
    return result;
  } finally {
    controllers.delete(controller);
  }
}
export function post<T>(path: string, body: unknown) {
  return api<T>(path, { method: "POST", body: JSON.stringify(body) });
}
