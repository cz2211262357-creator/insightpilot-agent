import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Download,
  CheckCheck,
  FileSearch,
  ChevronLeft,
  ChevronRight,
  Database,
} from "lucide-react";
import type { Evidence, Report, Catalog } from "../api/types";
import { api } from "../api/client";
import {
  downloadJson,
  ErrorNotice,
  Loading,
  metricNames,
  shortId,
} from "../components/Common";
import { Plot } from "../components/Plot";

function EvidenceTable({
  source,
  runId,
}: {
  source: Report["evidence"][number];
  runId: string;
}) {
  const [page, setPage] = useState(0);
  const [showSql, setShowSql] = useState(false);
  const detail = useQuery({
    queryKey: ["evidence", runId, source.evidence_id],
    queryFn: () =>
      api<Evidence>(`/runs/${runId}/evidence/${source.evidence_id}`),
    enabled: showSql,
    staleTime: 0,
  });
  const start = page * 25;
  const rows = source.rows.slice(start, start + 25);
  return (
    <section className="evidence-block">
      <div className="section-heading">
        <h4>
          <Database size={15} />
          查询证据 <span className="mono">{shortId(source.evidence_id)}</span>
        </h4>
        <button
          className="text-button"
          onClick={() => {
            setShowSql(!showSql);
            if (!showSql) void detail.refetch();
          }}
        >
          <FileSearch size={14} />
          {showSql ? "收起查询" : "查看查询与口径"}
        </button>
      </div>
      {showSql && (
        <div className="sql-detail">
          {detail.isLoading ? (
            <Loading />
          ) : detail.error ? (
            <ErrorNotice error={detail.error} />
          ) : (
            detail.data && (
              <>
                <pre>{detail.data.sql}</pre>
                {Object.keys(detail.data.params).length > 0 && (
                  <pre>{JSON.stringify(detail.data.params, null, 2)}</pre>
                )}
                {detail.data.metric_definitions.map((metric) => (
                  <p key={metric.metric_id}>
                    <strong>{metric.name}：</strong>
                    {metric.definition}
                  </p>
                ))}
                <span className="muted tiny">
                  查询摘要 {source.sql_hash.slice(0, 16)} · 口径版本{" "}
                  {detail.data.catalog_version}
                </span>
              </>
            )
          )}
        </div>
      )}
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              {source.columns.map((column) => (
                <th key={column}>{metricNames[column] ?? column}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={start + index}>
                {source.columns.map((column) => (
                  <td key={column}>
                    {row[column] === null ? (
                      <span className="muted">暂无数据</span>
                    ) : (
                      String(row[column] ?? "")
                    )}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {source.rows.length === 0 && (
          <p className="table-empty">所选条件下没有数据。</p>
        )}
      </div>
      <div className="table-footer">
        <span>
          {source.count} 行
          {source.truncated ? " · 结果已截断，不能作为全量合计" : ""}
        </span>
        <span className="mono">数据版本 {shortId(source.dataset_version)}</span>
        {source.count > 25 && (
          <span className="pagination">
            <button
              aria-label="上一页数据"
              disabled={!page}
              onClick={() => setPage((p) => p - 1)}
            >
              <ChevronLeft size={15} />
            </button>
            {page + 1} / {Math.ceil(source.count / 25)}
            <button
              aria-label="下一页数据"
              disabled={start + 25 >= source.count}
              onClick={() => setPage((p) => p + 1)}
            >
              <ChevronRight size={15} />
            </button>
          </span>
        )}
      </div>
    </section>
  );
}
export function ReportPanel({
  runId,
  catalog,
}: {
  runId: string;
  catalog?: Catalog;
}) {
  const query = useQuery({
    queryKey: ["report", runId],
    queryFn: () => api<Report>(`/runs/${runId}/report`),
    staleTime: 0,
    gcTime: 0,
    refetchOnMount: "always",
  });
  const [exportError, setExportError] = useState<unknown>();
  const [exporting, setExporting] = useState(false);
  async function exportReport() {
    setExporting(true);
    setExportError(undefined);
    try {
      const report = await api<Report>(`/runs/${runId}/report`);
      downloadJson(`InsightPilot-${shortId(runId)}.json`, {
        application: "InsightPilot",
        data_kind: "synthetic",
        as_of: catalog?.as_of,
        exported_at: new Date().toISOString(),
        report,
      });
    } catch (error) {
      setExportError(error);
    } finally {
      setExporting(false);
    }
  }
  if (query.isLoading || query.isFetching)
    return <Loading text="正在核验报告证据" />;
  if (query.error)
    return (
      <ErrorNotice error={query.error} retry={() => void query.refetch()} />
    );
  const report = query.data;
  if (!report) return null;
  return (
    <div className="report-panel">
      <div className="report-heading">
        <span>
          <CheckCheck size={16} />
          结论已通过证据校验
        </span>
        <button
          className="button secondary small"
          disabled={exporting}
          onClick={() => void exportReport()}
        >
          <Download size={14} />
          {exporting ? "核验中" : "导出报告与证据"}
        </button>
      </div>
      {exportError !== undefined && <ErrorNotice error={exportError} />}
      <div className="report-summary">
        {report.summary.split("\n").map((line, index) => (
          <p key={index}>{line}</p>
        ))}
      </div>
      {report.warnings.map((warning) => (
        <p className="notice warning" key={warning}>
          {warning}
        </p>
      ))}
      {report.charts
        .filter(
          (figure) =>
            !Array.isArray(figure.data) ||
            !figure.data.every(
              (trace) =>
                trace && typeof trace === "object" && trace.type === "table",
            ),
        )
        .map((figure, index) => (
          <Plot key={index} figure={figure} />
        ))}
      {report.evidence.map((source) => (
        <EvidenceTable key={source.evidence_id} source={source} runId={runId} />
      ))}
      <p className="report-provenance">
        指标口径 {report.catalog_version} · 关键数值均可追溯到查询证据 ·
        图表配置与数据版本随报告一同导出
      </p>
    </div>
  );
}
