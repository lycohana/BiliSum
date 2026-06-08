export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function routeMeta(route) {
  if (route === "settings") {
    return { eyebrow: "设置页", title: "VidMind 配置与后端信息" };
  }
  return { eyebrow: "视频库", title: "VidMind 视频库" };
}

export function formatDateTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

export function formatDuration(seconds) {
  if (!seconds && seconds !== 0) return "-";
  const total = Math.max(0, Math.floor(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  }
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

export function formatTaskDuration(seconds) {
  if (!seconds && seconds !== 0) return "-";
  const total = Math.max(0, Math.round(Number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}小时${minutes}分${secs}秒`;
  }
  if (minutes > 0) {
    return `${minutes}分${secs}秒`;
  }
  return `${secs}秒`;
}

export function formatTokenCount(value) {
  if (value == null || value === "") return "-";
  const number = Number(value);
  if (Number.isNaN(number)) return String(value);
  return number.toLocaleString("zh-CN");
}

export function taskStatusLabel(status) {
  const map = {
    queued: "排队中",
    running: "处理中",
    completed: "已完成",
    failed: "失败",
    cancelled: "已取消",
  };
  return map[status] || status || "未开始";
}

export function parseAppLocation(pathname) {
  if (pathname.startsWith("/videos/")) {
    return {
      route: "library",
      page: "video-detail",
      videoId: pathname.split("/videos/")[1] || null,
    };
  }
  return {
    route: pathname.startsWith("/settings") ? "settings" : "library",
    page: "library",
    videoId: null,
  };
}
