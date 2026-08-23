import type { CollectionGroupMode, CollectionSortMode, VideoCollectionItem } from "./types";

export type CollectionStatusFilter = "all" | "completed" | "processing" | "pending" | "failed";
export type CollectionItemStatus = Exclude<CollectionStatusFilter, "all">;
export type CollectionQueueOrder = "source" | "source_desc" | "current_view" | "selection";

export type CollectionViewOptions = {
  status: CollectionStatusFilter;
  query: string;
  sort: CollectionSortMode;
  group: CollectionGroupMode;
};

export type CollectionViewGroup = {
  key: string;
  title: string;
  items: VideoCollectionItem[];
};

export type CollectionViewResult = {
  sourceItems: VideoCollectionItem[];
  visibleItems: VideoCollectionItem[];
  groups: CollectionViewGroup[];
};

const STATUS_GROUP_ORDER: CollectionItemStatus[] = ["completed", "processing", "pending", "failed"];

export const COLLECTION_STATUS_LABELS: Record<CollectionStatusFilter, string> = {
  all: "全部",
  completed: "已总结",
  processing: "处理中",
  pending: "未总结",
  failed: "失败",
};

export const COLLECTION_SORT_LABELS: Record<CollectionSortMode, string> = {
  source: "原合集顺序",
  source_desc: "原合集倒序",
  recent_summary: "最近总结",
  recent_update: "最近更新",
  duration: "时长从长到短",
  custom: "自定义顺序",
};

export const COLLECTION_GROUP_LABELS: Record<CollectionGroupMode, string> = {
  none: "不分组",
  status: "按状态",
  source_section: "原合集分区",
};

export const COLLECTION_QUEUE_ORDER_LABELS: Record<CollectionQueueOrder, string> = {
  source: "按原合集顺序",
  source_desc: "按原合集倒序",
  current_view: "按当前展示顺序",
  selection: "按选择先后",
};

export function getCollectionItemStatus(item: VideoCollectionItem): CollectionItemStatus {
  if (item.has_result) return "completed";
  if (item.latest_status === "queued" || item.latest_status === "running") return "processing";
  if (item.latest_status === "failed" || item.latest_status === "cancelled") return "failed";
  return "pending";
}

export function isCollectionItemSummarizable(item: VideoCollectionItem): boolean {
  const status = getCollectionItemStatus(item);
  return Boolean(item.video_id) && (status === "pending" || status === "failed");
}

export function countCollectionStatuses(items: VideoCollectionItem[]): Record<CollectionStatusFilter, number> {
  const counts: Record<CollectionStatusFilter, number> = {
    all: items.length,
    completed: 0,
    processing: 0,
    pending: 0,
    failed: 0,
  };
  for (const item of items) counts[getCollectionItemStatus(item)] += 1;
  return counts;
}

export function sortBySourcePosition(items: VideoCollectionItem[], descending = false): VideoCollectionItem[] {
  return [...items].sort((left, right) => {
    const positionDelta = left.position - right.position;
    const stableDelta = positionDelta || left.bvid.localeCompare(right.bvid);
    return descending ? -stableDelta : stableDelta;
  });
}

export function deriveCollectionView(
  items: VideoCollectionItem[],
  options: CollectionViewOptions,
): CollectionViewResult {
  // Keep this order explicit: source -> status -> search -> sort -> group -> render.
  const sourceItems = sortBySourcePosition(items);
  const statusItems = options.status === "all"
    ? sourceItems
    : sourceItems.filter((item) => getCollectionItemStatus(item) === options.status);
  const queryItems = filterCollectionItemsByQuery(statusItems, options.query);
  const visibleItems = sortCollectionItems(queryItems, options.sort);
  return {
    sourceItems,
    visibleItems,
    groups: groupCollectionItems(visibleItems, options.group),
  };
}

export function filterCollectionItemsByQuery(items: VideoCollectionItem[], rawQuery: string): VideoCollectionItem[] {
  const query = normalizeSearchValue(rawQuery);
  if (!query) return items;
  const compactQuery = query.replace(/\s+/g, "");
  return items.filter((item) => {
    const searchable = normalizeSearchValue(`${item.title} ${item.bvid} 第 ${item.position} 集 第${item.position}集 ${item.position}`);
    return searchable.includes(query) || searchable.replace(/\s+/g, "").includes(compactQuery);
  });
}

export function sortCollectionItems(items: VideoCollectionItem[], sort: CollectionSortMode): VideoCollectionItem[] {
  switch (sort) {
    case "source_desc":
      return sortBySourcePosition(items, true);
    case "recent_summary":
      return stableSortByOptionalTimestamp(items, (item) => item.last_summary_at);
    case "recent_update":
      return stableSortByOptionalTimestamp(items, (item) => item.asset_updated_at);
    case "duration":
      return [...items].sort((left, right) => (
        (right.duration ?? -1) - (left.duration ?? -1)
        || left.position - right.position
        || left.bvid.localeCompare(right.bvid)
      ));
    case "custom":
      return [...items].sort((left, right) => (
        (left.custom_position ?? left.position) - (right.custom_position ?? right.position)
        || left.position - right.position
        || left.bvid.localeCompare(right.bvid)
      ));
    case "source":
    default:
      return sortBySourcePosition(items);
  }
}

export function groupCollectionItems(items: VideoCollectionItem[], group: CollectionGroupMode): CollectionViewGroup[] {
  if (group === "none") return [{ key: "all", title: "合集内容", items }];
  if (group === "status") {
    return STATUS_GROUP_ORDER.map((status) => ({
      key: `status:${status}`,
      title: COLLECTION_STATUS_LABELS[status],
      items: items.filter((item) => getCollectionItemStatus(item) === status),
    })).filter((entry) => entry.items.length > 0);
  }

  const sectionMap = new Map<string, CollectionViewGroup & { position: number }>();
  for (const item of items) {
    const sectionId = item.source_section_id || "unsectioned";
    const current = sectionMap.get(sectionId);
    if (current) {
      current.items.push(item);
      continue;
    }
    sectionMap.set(sectionId, {
      key: `section:${sectionId}`,
      title: item.source_section_title?.trim() || "未分区",
      position: item.source_section_position ?? Number.MAX_SAFE_INTEGER,
      items: [item],
    });
  }
  return [...sectionMap.values()]
    .sort((left, right) => left.position - right.position || left.title.localeCompare(right.title))
    .map(({ position: _position, ...entry }) => entry);
}

export function flattenCollectionGroups(groups: CollectionViewGroup[]): VideoCollectionItem[] {
  return groups.flatMap((group) => group.items);
}

export function buildCollectionQueueVideoIds({
  sourceItems,
  currentViewItems,
  selectedIds,
  selectionOrder,
  order,
}: {
  sourceItems: VideoCollectionItem[];
  currentViewItems: VideoCollectionItem[];
  selectedIds: Set<string>;
  selectionOrder: string[];
  order: CollectionQueueOrder;
}): string[] {
  const eligible = (item: VideoCollectionItem): item is VideoCollectionItem & { video_id: string } => (
    Boolean(item.video_id) && selectedIds.has(item.video_id as string) && isCollectionItemSummarizable(item)
  );
  const sourceEligible = sortBySourcePosition(sourceItems).filter(eligible);
  const itemByVideoId = new Map(sourceEligible.map((item) => [item.video_id, item]));

  let orderedItems: Array<VideoCollectionItem & { video_id: string }>;
  switch (order) {
    case "source_desc":
      orderedItems = [...sourceEligible].reverse();
      break;
    case "current_view": {
      const visible = currentViewItems.filter(eligible);
      const visibleIds = new Set(visible.map((item) => item.video_id));
      orderedItems = [...visible, ...sourceEligible.filter((item) => !visibleIds.has(item.video_id))];
      break;
    }
    case "selection": {
      const selectedInOrder = selectionOrder
        .map((videoId) => itemByVideoId.get(videoId))
        .filter((item): item is VideoCollectionItem & { video_id: string } => Boolean(item));
      const orderedIds = new Set(selectedInOrder.map((item) => item.video_id));
      orderedItems = [...selectedInOrder, ...sourceEligible.filter((item) => !orderedIds.has(item.video_id))];
      break;
    }
    case "source":
    default:
      orderedItems = sourceEligible;
  }
  return orderedItems.map((item) => item.video_id);
}

function stableSortByOptionalTimestamp(
  items: VideoCollectionItem[],
  getTimestamp: (item: VideoCollectionItem) => string | null | undefined,
): VideoCollectionItem[] {
  return [...items].sort((left, right) => {
    const leftValue = parseTimestamp(getTimestamp(left));
    const rightValue = parseTimestamp(getTimestamp(right));
    return rightValue - leftValue || left.position - right.position || left.bvid.localeCompare(right.bvid);
  });
}

function parseTimestamp(value: string | null | undefined): number {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : Number.NEGATIVE_INFINITY;
}

function normalizeSearchValue(value: string): string {
  return value.trim().toLocaleLowerCase();
}
