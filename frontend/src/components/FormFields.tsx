import { Plus, Search, X } from "lucide-react";
import { useState, type KeyboardEvent } from "react";

import { api } from "../lib/api";
import type { Region } from "../types";

export function TagInput({ label, value, onChange, placeholder, error }: { label: string; value: string[]; onChange: (value: string[]) => void; placeholder: string; error?: string }) {
  const [draft, setDraft] = useState("");
  const [localError, setLocalError] = useState("");
  const add = () => {
    const item = draft.trim();
    if (!item) return;
    if (item.length > 100) { setLocalError("항목은 100자 이하여야 합니다."); return; }
    if (value.length >= 50) { setLocalError("더 이상 추가할 수 없습니다."); return; }
    const key = item.normalize("NFKC").replace(/\s+/g, " ").toLocaleLowerCase("ko-KR");
    if (value.some((existing) => existing.normalize("NFKC").replace(/\s+/g, " ").toLocaleLowerCase("ko-KR") === key)) { setDraft(""); return; }
    onChange([...value, item]); setDraft(""); setLocalError("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Enter" || event.key === ",") { event.preventDefault(); add(); }
  };
  return (
    <div className="field-group">
      <label>{label}</label>
      <div className="tag-input-row"><input value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKeyDown} placeholder={placeholder} /><button type="button" className="button button-secondary" onClick={add}><Plus size={16} />추가</button></div>
      {value.length > 0 && <div className="tag-list" aria-label={`${label} 목록`}>{value.map((item) => <span key={item}>{item}<button type="button" onClick={() => onChange(value.filter((current) => current !== item))} aria-label={`${item} 삭제`}><X size={13} /></button></span>)}</div>}
      {(error || localError) && <p className="field-error">{error || localError}</p>}
    </div>
  );
}

export function RegionSelector({ value, onChange, error }: { value: Array<Pick<Region, "code" | "name">>; onChange: (value: Array<Pick<Region, "code" | "name">>) => void; error?: string }) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Region[]>([]);
  const [message, setMessage] = useState("");
  const [loading, setLoading] = useState(false);
  const search = async () => {
    const term = query.trim();
    if (!term) { setMessage("지역 이름을 입력해 주세요."); return; }
    setLoading(true); setMessage("");
    try {
      const items = await api.regions(term);
      setResults(items); if (!items.length) setMessage("일치하는 공식 지역이 없습니다.");
    } catch { setMessage("지역을 검색하지 못했습니다."); }
    finally { setLoading(false); }
  };
  return (
    <div className="field-group">
      <label htmlFor="region-search">적용 가능 지역</label>
      <div className="tag-input-row"><input id="region-search" value={query} onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); void search(); } }} placeholder="시·도 또는 시·군·구 검색" /><button type="button" className="button button-secondary" onClick={() => void search()} disabled={loading}><Search size={16} />{loading ? "검색 중" : "검색"}</button></div>
      {results.length > 0 && <ul className="search-results" aria-label="지역 검색 결과">{results.map((region) => <li key={region.code}><button type="button" disabled={value.length >= 50} onClick={() => { if (!value.some((item) => item.code === region.code) && value.length < 50) onChange([...value, { code: region.code, name: region.name }]); setResults([]); setQuery(""); }}><strong>{region.name}</strong><span>{region.level === "SIDO" ? "시·도" : region.parentName}</span></button></li>)}</ul>}
      {message && <p className="field-help" role="status">{message}</p>}
      {value.length > 0 && <div className="tag-list">{value.map((region) => <span key={region.code}>{region.name}<button type="button" onClick={() => onChange(value.filter((item) => item.code !== region.code))} aria-label={`${region.name} 삭제`}><X size={13} /></button></span>)}</div>}
      {error && <p className="field-error">{error}</p>}
    </div>
  );
}
