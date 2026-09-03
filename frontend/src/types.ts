export type Verdict = "ELIGIBLE" | "NEEDS_CONFIRMATION" | "INELIGIBLE";
export type ConditionStatus = "PASS" | "UNKNOWN" | "FAIL" | "NOT_APPLICABLE";
export type InterestStatus = "INTERESTED" | "ON_HOLD" | "NOT_INTERESTED";
export type DecisionFreshness = "CURRENT" | "COMPANY_CHANGED" | "ANNOUNCEMENT_CHANGED" | "RECALCULATING";

export interface User { id: string; username: string }

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Array<{ loc?: Array<string | number>; path?: string; field?: string; reason?: string; message?: string }>;
  requestId?: string;
}

export interface Region {
  code: string;
  name: string;
  parentCode: string | null;
  parentName: string | null;
  level: "SIDO" | "SIGUNGU";
}

export type CompanyProfileInput = {
  companyName: string;
  businessEntityType: "SOLE_PROPRIETOR" | "CORPORATION" | null;
  organizationType: "FOR_PROFIT" | "NON_PROFIT" | "COOPERATIVE" | "PRODUCER_ORGANIZATION" | null;
  companyScale: "MICRO" | "SMALL" | "MEDIUM" | "MID_SIZED" | "LARGE" | "UNKNOWN" | null;
  foundedOn: string | null;
  eligibleRegions: Array<Pick<Region, "code" | "name">>;
  primaryIndustry: string | null;
  secondaryIndustries: string[];
  annualRevenue: number | null;
  employeeCount: number | null;
  delinquencyStatus: "NONE" | "PRESENT" | null;
  certifications: string[];
  supportHistory: Array<{ programName: string; year: number }>;
  capabilityTags: string[];
  interestKeywords: string[];
  excludedKeywords: string[];
};

export interface CompanyProfileView extends CompanyProfileInput {
  id?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface CompanyResponse { profile: CompanyProfileView | null; version: number }

export interface AnnouncementListItem {
  id: string;
  announcementVersionId: string;
  companyProfileVersionId: string | null;
  title: string;
  organization: string;
  applicationStartDate: string | null;
  applicationEndDate: string | null;
  recruitmentStatus: "OPEN" | "CLOSED" | "UNKNOWN";
  verdict: Verdict;
  verdictReason: string;
  interestStatus: InterestStatus | null;
  decisionFreshness: DecisionFreshness;
  publishedAt?: string | null;
}

export interface AnnouncementListResponse { items: AnnouncementListItem[]; page: number; pageSize: 10; total: number }

export interface Evidence {
  sourceFileId?: string | null;
  sourceVersion?: string | null;
  sourceName: string;
  page: number | null;
  verbatimText: string;
}

export interface ConditionResult {
  conditionId: string;
  kind: "MANDATORY" | "PREFERENCE" | "GUIDANCE" | "POST_AWARD";
  label: string;
  status: ConditionStatus;
  explanation: string;
  assumptionCode?: string | null;
  evidence: Evidence[];
}

export interface RoleEstimate { roleKey: string; label: string; verdict: Verdict }

export interface AnnouncementQuestion {
  conditionId: string;
  question: string;
  valueType: "TEXT" | "BOOLEAN" | "NUMBER" | "DATE" | "SELECT";
  options?: Array<{ value: string; label: string }>;
  unit?: string | null;
  evidence: Evidence[];
  answered?: boolean;
}

export interface SourceFile {
  id: string;
  name: string;
  size: number | null;
  downloadStatus: "PENDING" | "SUCCEEDED" | "FAILED_RETRYABLE" | "FAILED_FINAL" | "LIMIT_EXCEEDED";
  extractionStatus: "PENDING" | "SUCCEEDED" | "FAILED_RETRYABLE" | "FAILED_FINAL" | "SKIPPED";
  failureReason?: string | null;
}

export interface AnnouncementDetail extends AnnouncementListItem {
  summary: string;
  resultExplanation: string;
  passedTrackLabels: string[];
  selectedRoleKey: string | null;
  roleEstimates: RoleEstimate[];
  conditions: ConditionResult[];
  questions: AnnouncementQuestion[];
  files: SourceFile[];
  sourceUrl: string;
  description?: string | null;
}

export interface QueueResponse { requestId: string; status: "QUEUED" }
