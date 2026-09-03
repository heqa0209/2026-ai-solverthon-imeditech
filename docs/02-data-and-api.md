# 데이터와 API 계약

이 문서는 영속 데이터, 버전 관계, 인증과 HTTP wire contract를 소유한다. 판정 의미는 [01-product-and-domain.md](01-product-and-domain.md), 작업 실행은 [03-ingestion-and-ai.md](03-ingestion-and-ai.md)를 따른다.

## 데이터 모델

이름은 코드 관례에 맞춰 snake_case로 구현하되 다음 관계와 불변성을 유지한다.

- `users`, `sessions`: 관리자가 만든 사용자와 Secure HttpOnly 세션
- `company_profiles`: 사용자별 현재 기업정보 포인터
- `company_profile_versions`: 저장 시점의 불변 전체 스냅샷과 원 입력 형태
- `announcements`: 기업마당 공고의 안정 식별자와 현재 버전 포인터
- `announcement_versions`: API 원본, 본문 해시, 모집 기간, 수집 시각과 원문 상태
- `source_files`: 파일 메타데이터, 원본 경로·해시·형식·크기·우선순위, 다운로드·추출 상태
- `extracted_conditions`: 유형·역할, 조건 그룹, 필수·우대·안내·이행 구분, 연산자, 값과 근거
- `analysis_runs`, `ai_stage_runs`: 공고버전별 파이프라인과 AI 단계의 구조화 실행 기록
- `eligibility_decisions`: 기업정보버전·공고버전·선택 역할별 계산 판정, 공개 판정과 공개 원인
- `condition_results`: 조건별 PASS·UNKNOWN·FAIL, 사용값, 근거와 완화 가정 코드
- `announcement_interests`: 사용자·공고별 관심 상태
- `announcement_role_selections`: 사용자·공고·공고버전별 불변 역할 선택 이력
- `announcement_answers`: 사용자·조건·공고버전별 답변과 출처
- `jobs`: idempotency key, lease, heartbeat, 재시도와 오류를 가진 작업 큐

발표된 판정 행은 수정하지 않는다. 새 분석·재판정은 새 행을 만들고 현재 포인터만 같은 트랜잭션에서 교체한다. 모든 사용자 소유 데이터는 `user_id` 또는 그에 이르는 FK를 가지며 기업정보·공고·판정 이력은 hard delete cascade 대상이 아니다.

기업마당 공고 ID, 작업 idempotency key, 사용자·공고 관심 상태에는 unique constraint를 둔다. 목록 필터, 현재 포인터, 공고버전·기업정보버전 조회와 작업 claim에 필요한 FK·상태·마감일 인덱스를 마이그레이션에 명시한다.

### 파일 상태

다운로드와 본문 추출은 서로 다른 상태로 저장한다.

- `download_status`: `PENDING | SUCCEEDED | FAILED_RETRYABLE | FAILED_FINAL | LIMIT_EXCEEDED`
- `extraction_status`: `PENDING | SUCCEEDED | FAILED_RETRYABLE | FAILED_FINAL | SKIPPED`

파일 본문을 받지 못해도 공개된 파일명, URL, 크기, 순서와 실패 사유는 보존한다.

## 기업정보 wire contract

`PUT /api/v1/company`는 다음 `CompanyProfileInput` 전체를 받는다. object의 알 수 없는 필드는 거부한다.

| 필드 | 형식 |
| --- | --- |
| `companyName` | 필수 문자열 |
| `businessEntityType` | `SOLE_PROPRIETOR \| CORPORATION \| null` |
| `organizationType` | `FOR_PROFIT \| NON_PROFIT \| COOPERATIVE \| PRODUCER_ORGANIZATION \| null` |
| `companyScale` | `MICRO \| SMALL \| MEDIUM \| MID_SIZED \| LARGE \| UNKNOWN \| null` |
| `foundedOn` | `YYYY-MM-DD \| null` |
| `eligibleRegions` | `{code, name}[]` |
| `primaryIndustry` | `string \| null` |
| `secondaryIndustries` | `string[]` |
| `annualRevenue` | 원 단위 정수 또는 null |
| `employeeCount` | 정수 또는 null |
| `delinquencyStatus` | `NONE \| PRESENT \| null` |
| `certifications` | `string[]` |
| `supportHistory` | `{programName, year}[]` |
| `capabilityTags` | `string[]` |
| `interestKeywords` | `string[]` |
| `excludedKeywords` | `string[]` |

명시적 `companyScale: UNKNOWN`과 null은 판정상 같은 UNKNOWN이지만 스냅샷에는 구분해 저장한다. `companyClassification`, `companySize`, `isSme` 등 이전 계약 필드는 호환하지 않고 422로 거부한다.

GET 응답과 PUT 입력은 별도 모델이다. `GET /company`가 반환하는 ID, 버전, 생성·수정 시각과 기타 서버 메타데이터를 PUT 본문에 포함하지 않는다. PUT은 `If-Match: "<version>"` 헤더로 낙관적 잠금을 적용하고 버전 불일치는 409로 반환한다.

프런트엔드는 매출을 쉼표와 `원`이 붙은 문자열로 표시하되 요청에는 숫자만 담긴 정수값을 보낸다. API와 DB 사이에서 날짜·금액을 임의 문자열로 바꾸지 않는다.

## 지역 데이터

canonical 원본은 `data/legal-regions-20260720.json`, provenance는 `data/legal-regions-20260720.meta.json`이다. provenance에는 원 출처 URL, 기준일, 생성일과 SHA-256을 기록한다. 구현 시 백엔드와 프런트엔드 배포 사본을 각각 `backend/src/app/data/`, `frontend/src/data/`에 생성하고 CI에서 canonical 원본과 byte-for-byte 일치를 검사한다.

선택 가능한 단위는 시·도와 시·군·구다. 시·도 선택은 하위 시·군·구를 포함한다. 공식 이름, 안전한 시·도 약칭과 전국에서 하나로 식별되는 축약명만 자동 정규화한다. `중구`처럼 동명 지역은 사용자가 검색 결과 후보를 선택해야 하며 문자열만으로 확정하지 않는다.

## 인증

- 사용자는 관리자가 CLI에서 미리 만들며 회원가입은 제공하지 않는다.
- 로그인 식별자는 정규화된 `username`이다. 공백 제거와 소문자 변환 후 `[a-z0-9._-]` 3~50자로 제한한다.
- 비밀번호는 Argon2id로 해시한다. session token은 256-bit 이상 난수이며 DB에는 token hash만 저장한다.
- 세션은 로그인 12시간 뒤 만료되고 로그아웃·비밀번호 초기화 시 폐기한다.
- 로그인 실패는 username+IP 기준 15분에 5회로 제한하고 초과 시 15분 잠근다.
- 동일-origin 쿠키는 `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`이다.
- 상태 변경은 세션에 결합된 `X-CSRF-Token`과 `Origin` 검사를 통과해야 한다.
- 별도 origin이 불가피할 때만 정확한 origin allowlist, credential CORS, CSRF와 `SameSite=None; Secure`를 함께 적용한다.

인증 응답은 다음으로 고정한다.

- 로그인과 `GET /auth/me`: `{user: {id, username}}`
- `GET /auth/csrf`: `{csrfToken}`
- 로그아웃 성공: body 없는 `204`

## Endpoint

| 메서드 | 경로 | 계약 |
| --- | --- | --- |
| POST | `/api/v1/auth/login` | username·password 로그인 |
| POST | `/api/v1/auth/logout` | 현재 세션 폐기 |
| GET | `/api/v1/auth/me` | 현재 사용자 확인 |
| GET | `/api/v1/auth/csrf` | 세션 결합 CSRF token 발급 |
| GET | `/api/v1/company` | 현재 기업정보와 서버 메타데이터 |
| PUT | `/api/v1/company` | `CompanyProfileInput` 전체 교체와 새 버전 생성 |
| GET | `/api/v1/company/versions` | 기업정보 버전 목록 |
| GET | `/api/v1/regions?query=` | 공식 지역 후보 검색 |
| GET | `/api/v1/announcements` | 공고 목록과 현재 사용자 판정 |
| GET | `/api/v1/announcements/{id}` | 공고·조건·판정·근거 상세 |
| GET | `/api/v1/announcements/{id}/files/{fileId}` | 권한 확인 후 저장 첨부 반환 |
| PUT | `/api/v1/announcements/{id}/interest` | 관심 상태 변경 |
| PUT | `/api/v1/announcements/{id}/role` | 역할 선택 이력 추가와 재판정 요청 |
| POST | `/api/v1/announcements/{id}/answers` | 공고별 답변 추가와 재판정 요청 |
| POST | `/api/v1/announcements/{id}/reevaluate` | 현재 입력으로 특정 공고 재판정 요청 |
| GET | `/api/v1/health/live` | 공개 liveness |
| GET | `/api/v1/ops/health/ready` | Access로 보호한 readiness |

`GET /company`는 항상 200을 반환한다. 아직 저장된 기업정보가 없으면 `{profile: null, version: 0}`과 `ETag: "0"`을 반환한다. 저장된 정보가 있으면 `{profile: CompanyProfileView, version}`과 현재 version ETag를 반환한다. 최초 PUT도 `If-Match: "0"`을 보내며 PUT 성공 응답은 같은 GET 형태와 새 ETag다.

관심 상태 PUT은 동기 저장 후 `200 {status, updatedAt}`을 반환한다. 역할·답변·재판정은 `202 {requestId, status: "QUEUED"}`를 반환한다. 별도 job 조회 API는 만들지 않으며 목록과 상세의 `decisionFreshness`, `announcementVersionId`, `companyProfileVersionId`로 새 결과 공개 여부를 제한적으로 확인한다.

지역 검색 응답은 `{items: [{code, name, parentCode, parentName, level}]}`이며 `level`은 `SIDO | SIGUNGU`다. 목록 item은 공고·현재 버전 ID, 제목, 기관, 모집기간·상태, 판정·한 줄 사유, 관심 상태와 `decisionFreshness`를 가진다. 상세는 여기에 요약·설명, 통과 유형, 선택 역할과 역할별 예상 판정, 조건 결과 전체, 질문, 원문 근거, 첨부 상태와 원문 URL을 추가한다.

모든 앱 endpoint는 로그인 사용자의 데이터만 반환한다. 볼 수 없는 리소스는 존재 여부를 숨기기 위해 404를 사용한다. 다운로드도 같은 권한 검사를 거친다.

### 비동기 재판정 요청

- 관심 상태: `{status}`. `status`는 `INTERESTED | ON_HOLD | NOT_INTERESTED`다.
- 역할: `{announcementVersionId, roleKey}`. `roleKey: null`은 현재 선택을 해제한다.
- 답변: `{announcementVersionId, conditionId, value, source, memo?}`. `source`는 `USER_VERIFIED | OFFICIAL_DOCUMENT | AGENCY_INQUIRY`다.
- 재판정: `{announcementVersionId}`.

세 endpoint는 작업을 enqueue하고 `202 {requestId, status: "QUEUED"}`를 반환한다. 같은 공고버전과 같은 입력의 반복 요청은 기존 idempotency key를 재사용한다. 현재 공고버전과 요청 버전이 다르면 409를 반환한다.

## 목록과 오류

공고 목록은 1부터 시작하는 `page`, 고정 `pageSize=10`을 사용한다. `keyword`, `eligibility`, `recruitmentStatus`, `interestStatus` 필터를 AND로 결합한다. 정렬은 마감일 null 후순위, 마감일 오름차순, 게시일 내림차순, 공고 ID 오름차순이다. 응답은 `{items, page, pageSize, total}`이다.

오류 응답은 `{code, message, details, requestId}`다. `details`는 필드 위치와 이유를 가진다.

- 401: 인증 실패 또는 세션 만료
- 403: 인증됐지만 작업 권한이 없음
- 404: 없음 또는 현재 사용자가 볼 수 없음
- 409: 버전·상태 충돌
- 422: 요청 검증 실패 또는 허용하지 않은 필드

## 버전과 재판정

기업정보를 저장할 때마다 불변 전체 스냅샷을 만든다. 기존 결과는 즉시 `기업정보 변경 전 결과`로 표시하고 모집 중이거나 기간을 해석하지 못한 모든 공고를 백그라운드에서 재판정한다. 새 결과가 준비되면 원자적으로 공개하고 이전 결과는 이력으로 남긴다.

마감 공고는 자동 재판정하지 않는다. 당시 기업정보와 판정을 역사적 결과로 유지하며 관리자는 CLI에서 특정 공고만 강제로 재판정할 수 있다.

최종 작업 실패를 공개할 때 계산이 끝났다면 `calculated_verdict`는 보존하고 `published_verdict=NEEDS_CONFIRMATION`, `decision_origin=SYSTEM_FAILURE`인 새 decision을 만든다. 계산 전 실패라면 `calculated_verdict`는 null이다. 실패 job은 삭제하지 않는다.
