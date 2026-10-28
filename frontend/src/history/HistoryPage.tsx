import { useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { Search, ChevronDown, ChevronRight } from "lucide-react";
import { api } from "../api/client";
import type { Catalog, RunPage } from "../api/types";
import {
  dateTime,
  Empty,
  ErrorNotice,
  Loading,
  PageTitle,
  StatusPill,
  labels,
} from "../components/Common";
import { ReportPanel } from "../workbench/ReportPanel";
export function HistoryPage({ catalog }: { catalog?: Catalog }) {
  const [text, setText] = useState("");
  const [state, setState] = useState("");
  const [opened, setOpened] = useState("");
  const query = useInfiniteQuery({
    queryKey: ["runs", "history"],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api<RunPage>(
        "/runs?limit=50" + (pageParam ? "&before=" + pageParam : ""),
      ),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const items = [
    ...new Map(
      (query.data?.pages.flatMap((page) => page.items) ?? []).map((run) => [
        run.run_id,
        run,
      ]),
    ).values(),
  ];
  const filtered = items.filter(
    (run) =>
      (!state || run.state === state) && run.question.includes(text.trim()),
  );
  return (
    <>
      <PageTitle
        eyebrow="ANALYSIS HISTORY"
        title="分析记录"
        description="回看每次分析的状态与证据，完成的报告可以再次核验和导出。"
      />
      <section className="panel">
        <div className="list-toolbar">
          <label className="search-field">
            <Search size={16} />
            <input
              aria-label="搜索分析问题"
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="搜索已加载的分析问题"
            />
          </label>
          <select
            aria-label="任务状态筛选"
            value={state}
            onChange={(e) => setState(e.target.value)}
          >
            <option value="">全部状态</option>
            {Object.entries(labels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        {query.isLoading ? (
          <Loading />
        ) : query.error ? (
          <ErrorNotice error={query.error} retry={() => void query.refetch()} />
        ) : filtered.length === 0 ? (
          <Empty title="暂无匹配的分析">
            可以回到工作台发起一个新问题，或调整筛选条件。
          </Empty>
        ) : (
          <div className="history-list">
            {filtered.map((run) => (
              <article key={run.run_id}>
                <button
                  className="history-row"
                  onClick={() =>
                    setOpened(opened === run.run_id ? "" : run.run_id)
                  }
                >
                  <span className="history-chevron">
                    {opened === run.run_id ? (
                      <ChevronDown size={16} />
                    ) : (
                      <ChevronRight size={16} />
                    )}
                  </span>
                  <div>
                    <strong>{run.question}</strong>
                    <small>
                      {dateTime(run.created_at)} ·{" "}
                      {run.scope === "BACKGROUND" ? "后台报告" : "即时分析"}
                    </small>
                  </div>
                  <StatusPill state={run.state} />
                  <span className="muted tiny">
                    模型 {run.model_used} · 查询 {run.query_used}
                  </span>
                </button>
                {opened === run.run_id && (
                  <div className="history-detail">
                    {run.state === "SUCCEEDED" && run.has_report ? (
                      <ReportPanel runId={run.run_id} catalog={catalog} />
                    ) : (
                      <p className="muted">
                        {run.report_error_code
                          ? "当前数据版本不可访问，报告已隐藏。"
                          : "这条任务尚无已完成的报告，可在工作台查看任务状态。"}
                      </p>
                    )}
                  </div>
                )}
              </article>
            ))}
          </div>
        )}
        <div className="list-footer">
          <span>已加载 {items.length} 条任务</span>
          {query.hasNextPage && (
            <button
              className="button secondary small"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              加载更多
            </button>
          )}
        </div>
      </section>
    </>
  );
}
