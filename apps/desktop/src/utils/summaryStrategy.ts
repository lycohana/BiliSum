import type { ServiceSettings } from "../types";

export type SummaryStrategy = "speed" | "cache" | "custom";
export type PresetSummaryStrategy = Exclude<SummaryStrategy, "custom">;

type SummaryStrategySettings = Pick<
  ServiceSettings,
  "summary_chunk_concurrency" | "summary_context_mode"
>;

export const SUMMARY_STRATEGY_COPY: Record<
  PresetSummaryStrategy,
  {
    title: string;
    description: string;
    contextLabel: string;
  }
> = {
  speed: {
    title: "速度优先",
    description: "并行处理摘要分块，短内容自动整段发送。适合更在意完成时间。",
    contextLabel: "自动上下文",
  },
  cache: {
    title: "缓存优先",
    description: "串行处理摘要分块并强制分块流程，给提供商更多复用固定前缀的机会。",
    contextLabel: "串行分块",
  },
};

export function getSummaryStrategy(
  settings: SummaryStrategySettings,
): SummaryStrategy {
  const matchesSpeed =
    settings.summary_chunk_concurrency === 2 &&
    (settings.summary_context_mode || "auto") === "auto";
  if (matchesSpeed) {
    return "speed";
  }

  const matchesCache =
    settings.summary_chunk_concurrency === 1 &&
    settings.summary_context_mode === "chunked";
  return matchesCache ? "cache" : "custom";
}

export function getSummaryStrategyPatch(
  strategy: PresetSummaryStrategy,
): SummaryStrategySettings {
  if (strategy === "cache") {
    return {
      summary_chunk_concurrency: 1,
      summary_context_mode: "chunked",
    };
  }

  return {
    summary_chunk_concurrency: 2,
    summary_context_mode: "auto",
  };
}
