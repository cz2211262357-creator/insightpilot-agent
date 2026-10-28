import { BookOpen, Database, Clock3 } from "lucide-react";
import type { Catalog } from "../api/types";
import { Loading, PageTitle } from "../components/Common";
const grainNames: Record<string, string> = {
  order: "按订单统计",
  "independent order and resolved-refund aggregates":
    "订单与已完成退款分别汇总后计算",
  "paid-order cohort": "按支付期间构建订单集合",
  aftersale: "按售后申请统计",
  "resolved aftersale": "按已完成售后统计",
};
const emptyRules: Record<string, string> = {
  paid_sales: "无匹配数据时显示“暂无数据”，与金额为零区分。",
  net_sales: "两个分量均无数据时显示“暂无数据”；仅缺一项时按零计算。",
  order_count: "无匹配订单时，订单量为零。",
  aftersale_rate: "分母为零时不计算比例，显示“暂无数据”。",
  refund_amount: "无匹配退款时显示“暂无数据”，与退款额为零区分。",
  avg_resolution_hours: "无已完成售后时，显示“暂无数据”。",
};
export function CatalogPage({ catalog }: { catalog?: Catalog }) {
  if (!catalog) return <Loading />;
  return (
    <>
      <PageTitle
        eyebrow="METRIC CATALOG"
        title="指标目录"
        description="先统一计算口径，再比较经营数据。所有分析都以当前目录为依据。"
      />
      <div className="notice info">
        <Database size={17} />
        <span>
          当前使用虚构经营数据集，快照为{" "}
          {catalog.as_of
            ? new Date(catalog.as_of).toLocaleDateString("zh-CN", {
                timeZone: "Asia/Shanghai",
              })
            : "未提供"}
          ，目录版本 {catalog.version}。
        </span>
      </div>
      <div className="metric-grid">
        {catalog.metrics.map((metric) => (
          <article className="panel metric-card" key={metric.metric_id}>
            <span className="metric-icon">
              <BookOpen size={20} />
            </span>
            <h2>{metric.name}</h2>
            <span className="mono muted tiny">{metric.metric_id}</span>
            <p>{metric.definition}</p>
            <div>
              <span>
                <Clock3 size={13} />
                {metric.timezone}
              </span>
              <span>统计粒度：{grainNames[metric.grain] ?? metric.grain}</span>
            </div>
            <footer>
              空结果处理：{emptyRules[metric.metric_id] ?? metric.empty_result}
            </footer>
          </article>
        ))}
      </div>
      <section className="panel view-catalog">
        <div className="section-heading">
          <h2>可查询的数据视图</h2>
          <span className="count">{Object.keys(catalog.views).length}</span>
        </div>
        <p className="muted">分析只能在已授权的视图与字段范围内执行。</p>
        {Object.entries(catalog.views).map(([view, columns]) => (
          <div className="view-row" key={view}>
            <strong className="mono">{view}</strong>
            <p>
              {columns.map((column) => (
                <code key={column}>{column}</code>
              ))}
            </p>
          </div>
        ))}
      </section>
    </>
  );
}
