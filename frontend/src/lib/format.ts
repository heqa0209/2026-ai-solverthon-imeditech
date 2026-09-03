export function formatDate(value: string | null | undefined): string {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "확인 필요";
  const [year, month, day] = value.split("-").map(Number);
  const date = new Date(year, month - 1, day);
  if (date.getFullYear() !== year || date.getMonth() !== month - 1 || date.getDate() !== day) return "확인 필요";
  return `${year}. ${month}. ${day}.`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) return "확인 필요";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "확인 필요";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
}

export function formatMoney(value: number | null | undefined): string { return value == null ? "" : `${value.toLocaleString("ko-KR")}원`; }

export function formatFileSize(value: number | null): string {
  if (value == null) return "크기 확인 필요";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}
