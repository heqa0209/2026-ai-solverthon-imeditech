export type Verdict = "ELIGIBLE" | "NEEDS_CONFIRMATION" | "INELIGIBLE";
export type ConditionStatus = "PASS" | "UNKNOWN" | "FAIL" | "NOT_APPLICABLE";
export type InterestStatus = "INTERESTED" | "ON_HOLD" | "NOT_INTERESTED";
export type DecisionFreshness = "CURRENT" | "COMPANY_PROFILE_CHANGED" | "ANNOUNCEMENT_CHANGED";

export interface User { id: string; username: string }

export interface ApiErrorBody {
  code: string;
  message: string;
  details?: Array<{ location?: Array<string | number>; loc?: Array<string | number>; path?: string; field?: string; reason?: string; message?: string }>;
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
  agencyName: string | null;
  recruitmentStartsOn: string | null;
  recruitmentEndsOn: string | null;
  recruitmentStatus: "OPEN" | "CLOSED" | "UNKNOWN";
  eligibility: Verdict;
  reason: string;
  interestStatus: InterestStatus | null;
  decisionFreshness: DecisionFreshness;
  publishedAt?: string | null;
}

export interface AnnouncementListResponse { items: AnnouncementListItem[]; page: number; pageSize: 10; total: number }

export interface Evidence {
  sourceFileId?: string | null;
  sourceVersion?: string | null;
  sourceName: string | null;
  page: number | null;
  verbatimText: string;
}

export interface ConditionResult {
  id: string;
  conditionKey: string;
  groupKey: string;
  trackKey: string | null;
  roleKey: string | null;
  kind: "MANDATORY" | "PREFERENCE" | "GUIDANCE" | "POST_AWARD";
  subject: string;
  operator: string;
  expectedValue: Record<string, unknown> | null;
  unit: string | null;
  referenceDate: string | null;
  status: ConditionStatus;
  usedValue: Record<string, unknown> | null;
  explanation: string | null;
  assumptionCode?: string | null;
  evidence: Evidence[];
}

export interface RolePrediction { roleKey: string; label: string; eligibility: Verdict | null }

export interface AnnouncementQuestion {
  conditionId: string;
  prompt: string;
  valueType: string;
  options: string[] | null;
  unit?: string | null;
  evidence: Evidence[];
}

export interface SourceFile {
  id: string;
  name: string;
  sourceUrl: string;
  sizeBytes: number | null;
  mimeType: string | null;
  sourceOrder: number;
  downloadStatus: "PENDING" | "SUCCEEDED" | "FAILED_RETRYABLE" | "FAILED_FINAL" | "LIMIT_EXCEEDED";
  extractionStatus: "PENDING" | "SUCCEEDED" | "FAILED_RETRYABLE" | "FAILED_FINAL" | "SKIPPED";
  failureCode: string | null;
}

export interface AnnouncementDetail extends AnnouncementListItem {
  publishedOn: string | null;
  summary: string | null;
  explanation: string | null;
  passedTrackKey: string | null;
  selectedRoleKey: string | null;
  rolePredictions: RolePrediction[];
  conditions: ConditionResult[];
  questions: AnnouncementQuestion[];
  files: SourceFile[];
  sourceUrl: string;
}

export interface QueueResponse { requestId: string; status: "QUEUED" }
