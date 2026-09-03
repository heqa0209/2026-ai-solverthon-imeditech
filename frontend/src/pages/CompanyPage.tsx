import { RefreshCw, Save, Trash2 } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import { AppShell } from "../components/AppShell";
import { RegionSelector, TagInput } from "../components/FormFields";
import { ErrorState, LoadingState } from "../components/States";
import { api, ApiError } from "../lib/api";
import type { CompanyProfileInput, CompanyProfileView } from "../types";

type Draft = Omit<CompanyProfileInput, "annualRevenue" | "employeeCount"> & { annualRevenue: string; employeeCount: string };
type Errors = Record<string, string>;

const emptyDraft: Draft = {
  companyName: "", businessEntityType: null, organizationType: null, companyScale: null, foundedOn: null,
  eligibleRegions: [], primaryIndustry: null, secondaryIndustries: [], annualRevenue: "", employeeCount: "",
  delinquencyStatus: null, certifications: [], supportHistory: [], capabilityTags: [], interestKeywords: [], excludedKeywords: [],
};

function toDraft(profile: CompanyProfileView | null): Draft {
  if (!profile) return { ...emptyDraft };
  return { ...profile, annualRevenue: profile.annualRevenue == null ? "" : profile.annualRevenue.toLocaleString("ko-KR"), employeeCount: profile.employeeCount == null ? "" : String(profile.employeeCount) };
}

function digits(value: string) { return value.replace(/[^0-9]/g, ""); }

export function validateCompany(draft: Draft): Errors {
  const errors: Errors = {};
  const name = draft.companyName.trim();
  if (!name) errors.companyName = "기업명을 입력해 주세요.";
  else if (name.length > 100) errors.companyName = "기업명은 100자 이하여야 합니다.";
  if (draft.foundedOn) {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const selected = new Date(`${draft.foundedOn}T00:00:00`);
    if (Number.isNaN(selected.getTime()) || selected > today) errors.foundedOn = "설립일은 오늘보다 늦을 수 없습니다.";
  }
  if (draft.annualRevenue && BigInt(digits(draft.annualRevenue) || "0") > 100000000000000000n) errors.annualRevenue = "매출액이 허용 범위를 넘었습니다.";
  if (draft.employeeCount && Number(draft.employeeCount) > 10_000_000) errors.employeeCount = "상시근로자 수가 허용 범위를 넘었습니다.";
  draft.supportHistory.forEach((item, index) => {
    if (!item.programName.trim()) errors[`supportHistory.${index}.programName`] = "사업명을 입력해 주세요.";
    if (item.programName.trim().length > 100) errors[`supportHistory.${index}.programName`] = "사업명은 100자 이하여야 합니다.";
    const currentYear = new Date().getFullYear();
    if (item.year < 1900 || item.year > currentYear) errors[`supportHistory.${index}.year`] = `연도는 1900~${currentYear} 범위여야 합니다.`;
  });
  return errors;
}

export function buildCompanyInput(draft: Draft): CompanyProfileInput {
  return {
    companyName: draft.companyName.trim(), businessEntityType: draft.businessEntityType, organizationType: draft.organizationType,
    companyScale: draft.companyScale, foundedOn: draft.foundedOn || null,
    eligibleRegions: draft.eligibleRegions.map(({ code, name }) => ({ code, name })),
    primaryIndustry: draft.primaryIndustry?.trim() || null, secondaryIndustries: [...draft.secondaryIndustries],
    annualRevenue: draft.annualRevenue ? Number(digits(draft.annualRevenue)) : null,
    employeeCount: draft.employeeCount ? Number(digits(draft.employeeCount)) : null,
    delinquencyStatus: draft.delinquencyStatus, certifications: [...draft.certifications],
    supportHistory: draft.supportHistory.map(({ programName, year }) => ({ programName: programName.trim(), year })),
    capabilityTags: [...draft.capabilityTags], interestKeywords: [...draft.interestKeywords], excludedKeywords: [...draft.excludedKeywords],
  };
}

function detailField(detail: NonNullable<ApiError["body"]["details"]>[number]): string | null {
  if (detail.field) return detail.field;
  if (detail.path) return detail.path.replace(/^body\.?/, "");
  if (detail.loc) return detail.loc.filter((part) => part !== "body").join(".");
  return null;
}

export function CompanyPage() {
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [etag, setEtag] = useState('"0"');
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  const [notice, setNotice] = useState("");
  const [conflict, setConflict] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    setLoading(true); setLoadError("");
    try { const result = await api.company(); setDraft(toDraft(result.profile)); setEtag(result.etag); setConflict(false); }
    catch (error) { setLoadError(error instanceof ApiError ? error.body.message : "기업정보를 불러오지 못했습니다."); }
    finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, []);

  const change = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    const next = { ...draft, [key]: value }; setDraft(next);
    const nextErrors = validateCompany(next); setErrors((current) => ({ ...current, [key]: nextErrors[key] || "" })); setNotice("");
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const found = validateCompany(draft); setErrors(found); setNotice(""); setConflict(false);
    if (Object.values(found).some(Boolean)) return;
    setSaving(true);
    try {
      const result = await api.updateCompany(buildCompanyInput(draft), etag);
      setDraft(toDraft(result.profile)); setEtag(result.etag); setNotice("기업정보를 저장했습니다. 진행 중인 공고 판정은 순서대로 갱신됩니다.");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) setConflict(true);
      else if (error instanceof ApiError && error.status === 422) {
        const fieldErrors: Errors = {};
        error.body.details?.forEach((detail) => { const field = detailField(detail); if (field) fieldErrors[field] = detail.reason || detail.message || error.body.message; });
        setErrors(fieldErrors); setNotice(error.body.message);
      } else setNotice(error instanceof ApiError ? error.body.message : "저장하지 못했습니다. 다시 시도해 주세요.");
    } finally { setSaving(false); }
  };

  if (loading) return <AppShell title="기업정보"><LoadingState label="기업정보를 불러오는 중입니다" /></AppShell>;
  if (loadError) return <AppShell title="기업정보"><ErrorState message={loadError} action={<button className="button button-secondary" onClick={() => void load()}>다시 시도</button>} /></AppShell>;

  return (
    <AppShell title="기업정보" description="기업명만 입력해도 저장할 수 있습니다. 비어 있는 값은 판정에서 확인 필요로 처리됩니다.">
      <form className="company-form" onSubmit={submit} noValidate>
        {conflict && <div className="notice notice-warning" role="alert"><div><strong>다른 곳에서 기업정보가 변경되었습니다.</strong><p>최신 정보를 다시 불러온 뒤 수정해 주세요.</p></div><button type="button" className="button button-secondary" onClick={() => void load()}><RefreshCw size={16} />최신 정보 불러오기</button></div>}
        {notice && <div className={Object.keys(errors).length ? "notice notice-error" : "notice notice-success"} role="status">{notice}</div>}

        <section className="form-card"><div className="section-heading"><span>01</span><div><h2>기본 정보</h2><p>사업자의 기본 속성을 입력합니다.</p></div></div><div className="form-grid">
          <div className="field-group field-wide"><label htmlFor="companyName">기업명 <b>*</b></label><input id="companyName" value={draft.companyName} onChange={(e) => change("companyName", e.target.value)} aria-invalid={Boolean(errors.companyName)} />{errors.companyName && <p className="field-error">{errors.companyName}</p>}</div>
          <label className="field-group">사업자 형태<select value={draft.businessEntityType || ""} onChange={(e) => change("businessEntityType", (e.target.value || null) as Draft["businessEntityType"])}><option value="">미확인</option><option value="SOLE_PROPRIETOR">개인사업자</option><option value="CORPORATION">법인</option></select></label>
          <label className="field-group">조직 속성<select value={draft.organizationType || ""} onChange={(e) => change("organizationType", (e.target.value || null) as Draft["organizationType"])}><option value="">미확인</option><option value="FOR_PROFIT">영리</option><option value="NON_PROFIT">비영리</option><option value="COOPERATIVE">협동조합</option><option value="PRODUCER_ORGANIZATION">생산자단체</option></select></label>
          <label className="field-group">기업규모<select value={draft.companyScale || ""} onChange={(e) => change("companyScale", (e.target.value || null) as Draft["companyScale"])}><option value="">선택하지 않음</option><option value="UNKNOWN">미확인</option><option value="MICRO">소상공인</option><option value="SMALL">소기업</option><option value="MEDIUM">중기업</option><option value="MID_SIZED">중견기업</option><option value="LARGE">대기업</option></select></label>
          <div className="field-group"><label htmlFor="foundedOn">설립일</label><input id="foundedOn" type="date" value={draft.foundedOn || ""} onChange={(e) => change("foundedOn", e.target.value || null)} aria-invalid={Boolean(errors.foundedOn)} />{errors.foundedOn && <p className="field-error">{errors.foundedOn}</p>}</div>
        </div></section>

        <section className="form-card"><div className="section-heading"><span>02</span><div><h2>지역과 업종</h2><p>공고의 지역·업종 제한과 비교합니다.</p></div></div><div className="form-grid">
          <div className="field-wide"><RegionSelector value={draft.eligibleRegions} onChange={(value) => change("eligibleRegions", value)} error={errors.eligibleRegions} /></div>
          <div className="field-group field-wide"><label htmlFor="primaryIndustry">주업종</label><input id="primaryIndustry" value={draft.primaryIndustry || ""} onChange={(e) => change("primaryIndustry", e.target.value || null)} placeholder="예: 의료기기 제조업" />{errors.primaryIndustry && <p className="field-error">{errors.primaryIndustry}</p>}</div>
          <div className="field-wide"><TagInput label="부업종" value={draft.secondaryIndustries} onChange={(value) => change("secondaryIndustries", value)} placeholder="업종을 입력하고 추가" error={errors.secondaryIndustries} /></div>
        </div></section>

        <section className="form-card"><div className="section-heading"><span>03</span><div><h2>규모와 자격</h2><p>최근 규모와 현재 자격 상태를 입력합니다.</p></div></div><div className="form-grid">
          <div className="field-group"><label htmlFor="annualRevenue">최근 매출액</label><div className="suffix-input"><input id="annualRevenue" inputMode="numeric" value={draft.annualRevenue} onChange={(e) => change("annualRevenue", digits(e.target.value).replace(/\B(?=(\d{3})+(?!\d))/g, ","))} aria-invalid={Boolean(errors.annualRevenue)} /><span>원</span></div>{errors.annualRevenue && <p className="field-error">{errors.annualRevenue}</p>}</div>
          <div className="field-group"><label htmlFor="employeeCount">상시근로자 수</label><div className="suffix-input"><input id="employeeCount" inputMode="numeric" value={draft.employeeCount} onChange={(e) => change("employeeCount", digits(e.target.value))} aria-invalid={Boolean(errors.employeeCount)} /><span>명</span></div>{errors.employeeCount && <p className="field-error">{errors.employeeCount}</p>}</div>
          <label className="field-group field-wide">체납 여부<select value={draft.delinquencyStatus || ""} onChange={(e) => change("delinquencyStatus", (e.target.value || null) as Draft["delinquencyStatus"])}><option value="">미확인</option><option value="NONE">체납 없음</option><option value="PRESENT">체납 있음</option></select></label>
          <div className="field-wide"><TagInput label="보유 인증" value={draft.certifications} onChange={(value) => change("certifications", value)} placeholder="인증명을 입력하고 추가" error={errors.certifications} /></div>
        </div></section>

        <section className="form-card"><div className="section-heading"><span>04</span><div><h2>이력과 역량</h2><p>지원 수혜 이력과 기업의 역량을 기록합니다.</p></div></div><div className="form-grid">
          <div className="field-wide field-group"><label>정부지원 수혜 이력</label>{draft.supportHistory.map((item, index) => <div className="history-row" key={`${index}-${item.year}`}><div><input aria-label={`수혜 사업명 ${index + 1}`} placeholder="사업명" value={item.programName} onChange={(e) => change("supportHistory", draft.supportHistory.map((current, itemIndex) => itemIndex === index ? { ...current, programName: e.target.value } : current))} />{errors[`supportHistory.${index}.programName`] && <p className="field-error">{errors[`supportHistory.${index}.programName`]}</p>}</div><div><input aria-label={`수혜 연도 ${index + 1}`} type="number" placeholder="연도" value={item.year || ""} onChange={(e) => change("supportHistory", draft.supportHistory.map((current, itemIndex) => itemIndex === index ? { ...current, year: Number(e.target.value) } : current))} />{errors[`supportHistory.${index}.year`] && <p className="field-error">{errors[`supportHistory.${index}.year`]}</p>}</div><button className="icon-button" type="button" onClick={() => change("supportHistory", draft.supportHistory.filter((_, itemIndex) => itemIndex !== index))} aria-label={`${index + 1}번 수혜 이력 삭제`}><Trash2 size={17} /></button></div>)}<button type="button" className="button button-secondary add-history" onClick={() => change("supportHistory", [...draft.supportHistory, { programName: "", year: new Date().getFullYear() }])}>수혜 이력 추가</button></div>
          <div className="field-wide"><TagInput label="제품·서비스·기술 태그" value={draft.capabilityTags} onChange={(value) => change("capabilityTags", value)} placeholder="역량을 입력하고 추가" error={errors.capabilityTags} /></div>
        </div></section>

        <section className="form-card"><div className="section-heading"><span>05</span><div><h2>탐색 선호</h2><p>목록 탐색에만 사용하며 자격 판정에는 영향을 주지 않습니다.</p></div></div><div className="form-grid"><div className="field-wide"><TagInput label="관심 키워드" value={draft.interestKeywords} onChange={(value) => change("interestKeywords", value)} placeholder="키워드를 입력하고 추가" /></div><div className="field-wide"><TagInput label="제외 키워드" value={draft.excludedKeywords} onChange={(value) => change("excludedKeywords", value)} placeholder="키워드를 입력하고 추가" /></div></div></section>

        <div className="form-submit"><button className="button button-primary button-large" disabled={saving || Object.values(errors).some(Boolean)}><Save size={18} />{saving ? "저장 중…" : "기업정보 저장"}</button></div>
      </form>
    </AppShell>
  );
}
