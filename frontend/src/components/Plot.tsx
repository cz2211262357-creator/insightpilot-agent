import { useEffect, useRef, useState } from "react";
import type { Data, Layout } from "plotly.js-dist-min";
export function Plot({ figure }: { figure: Record<string, unknown> }) {
  const div = useRef<HTMLDivElement>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const node = div.current;
    let active = true;
    let purge: (() => void) | undefined;
    let resize: ResizeObserver | undefined;
    if (!node) return;
    const data = figure.data;
    if (
      !Array.isArray(data) ||
      !data.every(
        (trace) =>
          trace &&
          typeof trace === "object" &&
          ["bar", "scatter", "table"].includes(String(trace.type)),
      )
    ) {
      setFailed(true);
      return;
    }
    void import("plotly.js-dist-min")
      .then(async ({ default: Plotly }) => {
        if (!active) return;
        await Plotly.react(
          node,
          data as Data[],
          { ...(figure.layout as Partial<Layout>), autosize: true },
          { responsive: false, displayModeBar: false, displaylogo: false },
        );
        purge = () => Plotly.purge(node);
        if (!active) {
          purge();
          return;
        }
        const visible = () =>
          active &&
          node.isConnected &&
          node.clientWidth > 0 &&
          node.clientHeight > 0;
        resize = new ResizeObserver(() => {
          if (visible())
            void Promise.resolve(Plotly.Plots.resize(node)).catch(() => {
              if (visible()) setFailed(true);
            });
        });
        resize.observe(node);
      })
      .catch(() => {
        if (active) setFailed(true);
      });
    return () => {
      active = false;
      resize?.disconnect();
      purge?.();
    };
  }, [figure]);
  return failed ? (
    <p className="muted">图表暂不可用，可查看下方证据表。</p>
  ) : (
    <div ref={div} className="plot" aria-label="分析图表" />
  );
}
