import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  FolderPlus,
  ArrowRight,
  Sparkles,
  Square,
  RefreshCw,
  MessageSquareText,
  ListChecks,
  CheckCircle2,
} from "lucide-react";
import {
  api,
  ApiError,
  errorText,
  post,
  sessionGeneration,
} from "../api/client";
import { watchRun } from "../api/events";
import type {
  Catalog,
  Conversation,
  ConversationItem,
  History,
  Receipt,
  Review,
  Run,
  RunPage,
  System,
  TopicSnapshot,
} from "../api/types";
import {
  dateTime,
  Empty,
  ErrorNotice,
  Loading,
  PageTitle,
  StatusPill,
  stages,
  metricNames,
} from "../components/Common";
import { Composer, type MessageDraft } from "./Composer";
import {
  applyEvent,
  applySnapshot,
  type RunRecord,
  type WorkState,
} from "./state";
import { SubmissionRegistry } from "./submission";
import { ReviewPanel, type DecisionRequest } from "./ReviewPanel";
import { ReportPanel } from "./ReportPanel";

const active = new Set(["QUEUED", "RUNNING", "WAITING_INPUT"]);
function selectedMetrics(run: RunRecord, catalog?: Catalog) {
  const ids = new Set(
    (run.public_plan ?? []).flatMap((step) =>
      Array.isArray(step.metric_ids) ? step.metric_ids : [],
    ),
  );
  return (
    catalog?.metrics.filter(
      (metric) => !ids.size || ids.has(metric.metric_id),
    ) ?? []
  );
}

function TaskDetail({
  run,
  catalog,
  system,
  onRefresh,
  onCancel,
  onRevise,
}: {
  run: RunRecord;
  catalog?: Catalog;
  system?: System;
  onRefresh: () => Promise<void>;
  onCancel: () => Promise<void>;
  onRevise: (draft: MessageDraft) => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const client = useQueryClient();
  const confirmation = useQuery({
    queryKey: ["review", run.run_id, run.task_revision],
    queryFn: () => api<Review>(`/runs/${run.run_id}/confirmation`),
    enabled: run.state === "WAITING_INPUT",
    staleTime: 0,
    retry: false,
  });
  async function cancel() {
    setBusy(true);
    setError(undefined);
    try {
      await onCancel();
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  }
  async function resume(body: DecisionRequest) {
    await post<Receipt>(`/runs/${run.run_id}/resume`, body);
    await onRefresh();
    await client.invalidateQueries({ queryKey: ["review", run.run_id] });
  }
  return (
    <section className="task-detail">
      <div className="detail-heading">
        <div>
          <span className="eyebrow">当前分析</span>
          <h2>{run.question ?? "分析任务"}</h2>
        </div>
        <StatusPill state={run.state} />
      </div>
      <div className="task-meta">
        <span>
          {run.scope === "BACKGROUND"
            ? "后台报告 · 可切换话题"
            : "即时分析 · 新消息可取代"}
        </span>
        {run.created_at && <span>{dateTime(run.created_at)}</span>}
        <button className="text-button" onClick={() => void onRefresh()}>
          <RefreshCw size={13} />
          刷新
        </button>
      </div>
      {(run.public_plan ?? []).length > 0 && (
        <div className="public-plan">
          <h3>
            <ListChecks size={16} />
            分析步骤
          </h3>
          <ol>
            {run.public_plan?.map((step, index) => (
              <li key={index}>
                <span>{index + 1}</span>
                <div>
                  <strong>
                    {typeof step.goal === "string" ? step.goal : "核验指标"}
                  </strong>
                  <small>
                    {Array.isArray(step.metric_ids)
                      ? step.metric_ids
                          .map((id) => metricNames[String(id)] ?? String(id))
                          .join(" · ")
                      : ""}
                  </small>
                </div>
              </li>
            ))}
          </ol>
        </div>
      )}
      <div className="budget-strip">
        <span>
          模型调用 <strong>{run.model_used ?? 0}</strong> /{" "}
          {system?.model_calls ?? 8}
        </span>
        <span>
          查询执行 <strong>{run.query_used ?? 0}</strong> /{" "}
          {system?.query_calls ?? 4}
        </span>
        <span>
          查询修正 <strong>{run.repair_used ?? 0}</strong> /{" "}
          {system?.repair_calls ?? 2}
        </span>
      </div>
      {run.state === "RUNNING" && (
        <div className="analysis-progress">
          <span className="pulse-dot" />
          {stages[run.stage ?? ""] ?? "正在执行分析"}
          <span className="muted">结果核验完成后会在这里发布</span>
        </div>
      )}
      {run.state === "QUEUED" && (
        <p className="notice info">
          任务已保存，正在等待执行名额。切换话题后，后台任务会继续保留。
        </p>
      )}
      {run.state === "WAITING_INPUT" &&
        (confirmation.isLoading ? (
          <Loading text="读取待确认事项" />
        ) : confirmation.error ? (
          <ErrorNotice
            error={confirmation.error}
            retry={() => void confirmation.refetch()}
          />
        ) : (
          confirmation.data && (
            <ReviewPanel
              key={confirmation.data.confirmation_id}
              run={run}
              review={confirmation.data}
              metrics={selectedMetrics(run, catalog)}
              onDecision={resume}
              onRefresh={() => {
                void onRefresh();
                void confirmation.refetch();
              }}
            />
          )
        ))}
      {run.state === "SUCCEEDED" &&
        (run.has_report ? (
          <ReportPanel runId={run.run_id} catalog={catalog} />
        ) : (
          <ErrorNotice
            error={
              new ApiError(403, run.report_error_code ?? "dataset_unavailable")
            }
          />
        ))}
      {["FAILED", "TIMED_OUT"].includes(run.state) && (
        <div className="notice error">
          <span>
            {errorText(
              new ApiError(409, run.error_code ?? "analysis_incomplete"),
            )}{" "}
            已保存执行记录，可调整问题后新建分析。
          </span>
        </div>
      )}
      {["CANCELLED", "SUPERSEDED"].includes(run.state) && (
        <p className="notice info">
          这条任务已停止。已完成的历史分析保留在分析记录中。
        </p>
      )}
      {error !== undefined && <ErrorNotice error={error} />}
      {active.has(run.state) && (
        <div className="task-actions">
          <button
            className="button secondary small"
            onClick={() => setEditing(!editing)}
          >
            <RefreshCw size={14} />
            {editing ? "收起修改" : "修改任务"}
          </button>
          <button
            className="button danger small"
            disabled={busy}
            onClick={() => void cancel()}
          >
            <Square size={12} />
            {busy ? "正在取消" : "取消这条任务"}
          </button>
        </div>
      )}
      {editing && active.has(run.state) && (
        <div className="revise-editor">
          <h4>修改问题后重新分析</h4>
          <p className="muted">
            新执行会取代这条未完成任务，其他话题保持各自的进度。
          </p>
          <Composer
            onSend={async (draft) => {
              await onRevise(draft);
              setEditing(false);
            }}
          />
        </div>
      )}
    </section>
  );
}

export function Workbench({
  catalog,
  system,
}: {
  catalog?: Catalog;
  system?: System;
}) {
  const at = sessionGeneration();
  const client = useQueryClient();
  const [conversationId, setConversationId] = useState("");
  const [work, setWork] = useState<WorkState>({
    generation: at,
    epoch: 0,
    topic_id: "",
    runs: {},
    cursors: {},
  });
  const submissions = useRef(new SubmissionRegistry());
  const live = useRef(work);
  live.current = work;
  const view = useRef(0);
  const streams = useRef(new Map<string, () => void>());
  const [selected, setSelected] = useState("");
  const [newTitle, setNewTitle] = useState("");
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [scope, setScope] = useState<"BACKGROUND" | "INTERACTIVE">(
    "BACKGROUND",
  );
  const conversations = useQuery({
    queryKey: ["conversations"],
    queryFn: () => api<ConversationItem[]>("/conversations"),
  });
  const conversation = useQuery({
    queryKey: ["conversation", conversationId],
    queryFn: () => api<Conversation>(`/conversations/${conversationId}`),
    enabled: Boolean(conversationId),
    refetchInterval: 3000,
  });
  const history = useQuery({
    queryKey: ["topic-history", work.topic_id],
    queryFn: () => api<History>(`/topics/${work.topic_id}/history`),
    enabled: Boolean(work.topic_id),
    refetchInterval: 4000,
  });
  const recent = useQuery({
    queryKey: ["runs", "workbench"],
    queryFn: () => api<RunPage>("/runs?limit=50"),
    refetchInterval: 3000,
  });
  useEffect(() => {
    if (!conversationId && conversations.data?.length) {
      setConversationId(conversations.data[0].conversation_id);
    }
  }, [conversationId, conversations.data]);
  useEffect(() => {
    const data = conversation.data;
    if (data && data.conversation_id === conversationId)
      setWork((current) =>
        data.epoch >= current.epoch
          ? { ...current, epoch: data.epoch, topic_id: data.active_topic_id }
          : current,
      );
  }, [conversation.data, conversationId]);
  useEffect(() => {
    if (history.data)
      setWork((current) =>
        history.data.runs.reduce(
          (state, run) => applySnapshot(state, run, at),
          current,
        ),
      );
  }, [history.data, at, work.topic_id, work.epoch]);
  useEffect(() => {
    if (recent.data)
      setWork((current) =>
        recent.data.items.reduce(
          (state, run) => applySnapshot(state, run, at),
          current,
        ),
      );
  }, [recent.data, at, work.topic_id, work.epoch]);
  const tasks = useMemo(
    () =>
      Object.values(work.runs)
        .filter((run) => run.topic_id === work.topic_id)
        .sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")),
    [work.runs, work.topic_id],
  );
  useEffect(() => {
    if (!tasks.some((run) => run.run_id === selected))
      setSelected(tasks[0]?.run_id ?? "");
  }, [tasks, selected]);
  async function refreshRun(id: string) {
    const run = await api<Run>(`/runs/${id}`);
    setWork((current) => applySnapshot(current, run, at));
    await client.invalidateQueries({ queryKey: ["runs", "workbench"] });
  }
  const watched = Object.values(work.runs)
    .filter((run) => active.has(run.state))
    .sort(
      (a, b) =>
        Number(b.topic_id === work.topic_id) -
        Number(a.topic_id === work.topic_id),
    )
    .slice(0, 10)
    .map((run) => run.run_id)
    .sort()
    .join(",");
  useEffect(() => {
    const ids = new Set(watched.split(",").filter(Boolean));
    for (const [id, stop] of streams.current)
      if (!ids.has(id)) {
        stop();
        streams.current.delete(id);
      }
    for (const id of ids)
      if (!streams.current.has(id))
        streams.current.set(
          id,
          watchRun(
            id,
            () => live.current.cursors[id] ?? 0,
            (event) => setWork((current) => applyEvent(current, event, at)),
            () => {
              void api<Run>(`/runs/${id}`)
                .then((run) =>
                  setWork((current) => applySnapshot(current, run, at)),
                )
                .catch(() => {});
            },
          ),
        );
  }, [watched, at]);
  useEffect(
    () => () => {
      for (const stop of streams.current.values()) stop();
      streams.current.clear();
    },
    [],
  );
  function chooseConversation(id: string) {
    view.current++;
    setConversationId(id);
    setSelected("");
    setWork((current) => ({ ...current, epoch: 0, topic_id: "" }));
    setError(undefined);
  }
  async function createConversation() {
    setBusy(true);
    setError(undefined);
    const navigation = ++view.current;
    try {
      const created = await post<TopicSnapshot>("/conversations", {
        title: "经营分析",
      });
      if (view.current === navigation) {
        setConversationId(created.conversation_id);
        setWork((current) => ({
          ...current,
          epoch: created.epoch,
          topic_id: created.active_topic_id,
        }));
        setSelected("");
      }
      await client.invalidateQueries({ queryKey: ["conversations"] });
    } catch (error) {
      setError(error);
    } finally {
      setBusy(false);
    }
  }
  async function switchTopic(id: string) {
    if (id === work.topic_id || busy) return;
    setBusy(true);
    setError(undefined);
    const navigation = ++view.current;
    try {
      const data = await post<TopicSnapshot>(
        `/conversations/${conversationId}/topics/${id}/activate`,
        { expected_epoch: live.current.epoch },
      );
      if (view.current === navigation)
        setWork((current) => ({
          ...current,
          epoch: Math.max(current.epoch, data.epoch),
          topic_id: data.active_topic_id,
        }));
      await conversation.refetch();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409)
        await conversation.refetch();
    } finally {
      setBusy(false);
    }
  }
  async function addTopic() {
    if (!conversationId || busy) return;
    setBusy(true);
    setError(undefined);
    const navigation = ++view.current;
    try {
      const data = await post<TopicSnapshot>(
        `/conversations/${conversationId}/topics`,
        {
          title: newTitle.trim() || "新分析话题",
          expected_epoch: live.current.epoch,
        },
      );
      if (view.current === navigation)
        setWork((current) => ({
          ...current,
          epoch: data.epoch,
          topic_id: data.active_topic_id,
        }));
      setAdding(false);
      setNewTitle("");
      await conversation.refetch();
    } catch (error) {
      setError(error);
      if (error instanceof ApiError && error.status === 409)
        await conversation.refetch();
    } finally {
      setBusy(false);
    }
  }
  async function send(draft: MessageDraft) {
    const navigation = view.current;
    const topic = live.current.topic_id;
    if (!conversationId || !topic) throw new Error("conversation_required");
    const frame = submissions.current.prepare({
      ...draft,
      conversation_id: conversationId,
      topic_id: topic,
      scope,
      expected_epoch: live.current.epoch,
    });
    try {
      const { conversation_id, ...body } = frame;
      const receipt = await post<Receipt>(
        `/conversations/${conversation_id}/messages`,
        body,
      );
      if (view.current === navigation && at === sessionGeneration()) {
        setWork((current) => ({
          ...current,
          epoch: Math.max(current.epoch, receipt.epoch),
        }));
        setSelected(receipt.run_id);
      }
      await refreshRun(receipt.run_id);
      void conversation.refetch();
      submissions.current.complete(draft.client_message_id);
    } catch (error) {
      if (error instanceof ApiError && error.code === "conversation_conflict")
        await conversation.refetch();
      throw error;
    }
  }

  const run = work.runs[selected];
  const observed =
    recent.data?.items.map((item) => work.runs[item.run_id] ?? item) ?? [];
  const running = observed.filter((r) => r.state === "RUNNING").length;
  const waiting = observed.filter((r) => r.state === "WAITING_INPUT").length;
  const done = observed.filter((r) => r.state === "SUCCEEDED").length;
  return (
    <>
      <PageTitle
        eyebrow="WORKSPACE"
        title="问数工作台"
        description="从业务问题出发，让每个结论都有数据依据。"
        action={
          <button
            className="button secondary"
            onClick={() => void createConversation()}
            disabled={busy}
          >
            <FolderPlus size={16} />
            新建会话
          </button>
        }
      />
      <div className="overview-stats">
        <div>
          <span className="stat-icon indigo">
            <Sparkles size={18} />
          </span>
          <span>
            <small>正在分析</small>
            <strong>{recent.data ? running : "—"}</strong>
          </span>
        </div>
        <div>
          <span className="stat-icon amber">
            <MessageSquareText size={18} />
          </span>
          <span>
            <small>等待确认</small>
            <strong>{recent.data ? waiting : "—"}</strong>
          </span>
        </div>
        <div>
          <span className="stat-icon green">
            <CheckCircle2 size={18} />
          </span>
          <span>
            <small>最近已完成</small>
            <strong>{recent.data ? done : "—"}</strong>
          </span>
        </div>
        <p>
          {recent.data
            ? "最近 " + recent.data.items.length + " 条任务"
            : "正在读取任务记录"}
          <br />
          <span>仅显示当前账号的数据</span>
        </p>
      </div>
      {recent.error && (
        <ErrorNotice error={recent.error} retry={() => void recent.refetch()} />
      )}{" "}
      {error !== undefined && <ErrorNotice error={error} />}{" "}
      {(conversation.error || history.error || conversations.error) && (
        <ErrorNotice
          error={conversation.error ?? history.error ?? conversations.error}
          retry={() => {
            void conversation.refetch();
            void history.refetch();
            void conversations.refetch();
          }}
        />
      )}
      {!conversationId ? (
        <section className="panel">
          <Empty
            title={
              conversations.isLoading ? "正在加载会话" : "从一个分析会话开始"
            }
            action={
              <button
                className="button primary"
                disabled={busy}
                onClick={() => void createConversation()}
              >
                创建分析会话
                <ArrowRight size={16} />
              </button>
            }
          >
            你可以按业务主题组织问题，后台分析与话题切换分别管理。
          </Empty>
        </section>
      ) : (
        <>
          <div className="workspace-toolbar">
            <label>
              分析会话
              <select
                aria-label="分析会话"
                value={conversationId}
                onChange={(e) => chooseConversation(e.target.value)}
              >
                {conversations.data?.map((item) => (
                  <option
                    key={item.conversation_id}
                    value={item.conversation_id}
                  >
                    经营分析 · {dateTime(item.created_at)}
                  </option>
                ))}
              </select>
            </label>
            <span className="muted tiny">切换话题会保留后台分析</span>
          </div>
          <div className="topic-tabs">
            {conversation.data?.topics.map((topic) => (
              <button
                key={topic.topic_id}
                className={topic.topic_id === work.topic_id ? "active" : ""}
                disabled={busy}
                onClick={() => void switchTopic(topic.topic_id)}
              >
                {topic.title}
              </button>
            ))}
            <button
              className="add-topic"
              aria-label="新建话题"
              disabled={busy}
              onClick={() => setAdding(!adding)}
            >
              <Plus size={15} />
            </button>
          </div>
          {adding && (
            <form
              className="new-topic-form"
              onSubmit={(e) => {
                e.preventDefault();
                void addTopic();
              }}
            >
              <input
                aria-label="话题名称"
                value={newTitle}
                onChange={(e) => setNewTitle(e.target.value)}
                maxLength={80}
                autoFocus
                placeholder="例如：渠道销售对比"
              />
              <button className="button primary small" disabled={busy}>
                创建话题
              </button>
              <button
                type="button"
                className="text-button"
                onClick={() => setAdding(false)}
              >
                取消
              </button>
            </form>
          )}
          <div className="analysis-layout">
            <div className="analysis-column">
              <section className="panel question-panel">
                <div className="section-heading">
                  <h2>
                    <Sparkles size={17} />
                    发起分析
                  </h2>
                  <select
                    aria-label="运行方式"
                    value={scope}
                    onChange={(e) => setScope(e.target.value as typeof scope)}
                  >
                    <option value="BACKGROUND">后台报告</option>
                    <option value="INTERACTIVE">即时分析</option>
                  </select>
                </div>
                <p className="muted">
                  {scope === "BACKGROUND"
                    ? "后台报告可在切换话题后继续；需要停止时，请取消对应任务。"
                    : "新消息或切换话题会取代当前即时回答。"}
                </p>
                <Composer
                  key={at + "-" + work.topic_id}
                  onSend={send}
                  disabled={!work.topic_id || busy}
                />
                <div className="question-examples">
                  <span>试着提问</span>
                  <p>“按渠道比较 2026 年八月与九月的实付销售额”</p>
                </div>
              </section>
              {run && run.topic_id === work.topic_id ? (
                <TaskDetail
                  key={run.run_id}
                  run={run}
                  catalog={catalog}
                  system={system}
                  onRefresh={() => refreshRun(run.run_id)}
                  onCancel={async () => {
                    await post(`/runs/${run.run_id}/cancel`, {
                      expected_revision: run.task_revision,
                    });
                    await refreshRun(run.run_id);
                  }}
                  onRevise={async (draft) => {
                    const receipt = await post<Receipt>(
                      `/runs/${run.run_id}/revise`,
                      { ...draft, expected_revision: run.task_revision },
                    );
                    await refreshRun(run.run_id);
                    await refreshRun(receipt.run_id);
                    setSelected(receipt.run_id);
                    void conversation.refetch();
                  }}
                />
              ) : (
                <section className="panel">
                  <Empty title="当前话题还没有分析">
                    说明时间、指标和筛选条件，任务会在这里显示进度与报告。
                  </Empty>
                  <div className="flow-preview">
                    <span>理解问题</span>
                    <ArrowRight size={14} />
                    <span>确认口径</span>
                    <ArrowRight size={14} />
                    <span>只读查询</span>
                    <ArrowRight size={14} />
                    <span>证据报告</span>
                  </div>
                </section>
              )}
            </div>
            <aside className="task-rail">
              <div className="section-heading">
                <h3>话题内任务</h3>
                <span className="count">{tasks.length}</span>
              </div>
              {tasks.length === 0 ? (
                <p className="muted">发起分析后，任务会保存在这里。</p>
              ) : (
                tasks.map((item) => (
                  <button
                    className={
                      "task-card " +
                      (selected === item.run_id ? "selected" : "")
                    }
                    key={item.run_id}
                    onClick={() => setSelected(item.run_id)}
                  >
                    <div>
                      <StatusPill state={item.state} />
                      <span>
                        {item.scope === "BACKGROUND" ? "后台" : "即时"}
                      </span>
                    </div>
                    <strong>{item.question ?? "分析任务"}</strong>
                    <small>
                      {item.created_at ? dateTime(item.created_at) : "刚刚提交"}
                    </small>
                  </button>
                ))
              )}
              <div className="rail-note">
                <ListChecks size={18} />
                <strong>让分析可复核</strong>
                <p>
                  报告保留指标口径、查询结果与数据版本。需要人工确认时，任务会暂停等你决定。
                </p>
              </div>
            </aside>
          </div>
        </>
      )}
    </>
  );
}
