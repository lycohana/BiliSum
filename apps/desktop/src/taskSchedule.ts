export function parseTaskSchedule(raw: string): string {
  const date = new Date(raw);
  if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) {
    throw new Error("请选择未来的开始时间。");
  }
  return date.toISOString();
}
