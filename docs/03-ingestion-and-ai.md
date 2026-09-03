# 수집과 AI 파이프라인 계약

이 문서는 공고 수집, 첨부파일, AI 단계, 작업 큐와 결과 공개를 소유한다. 판정 집계는 [01-product-and-domain.md](01-product-and-domain.md), 저장 관계는 [02-data-and-api.md](02-data-and-api.md)를 따른다.

## 공고 수집

- 출처는 기업마당 하나이며 크롤링하지 않는다.
- 매일 Asia/Seoul 오전 6시에 최근 등록·수정 공고를 수집하고 매주 한 번 전체 목록을 대조한다.
- 예정 시각에 Mac이 꺼져 있었다면 다음 서비스 실행 시 누락 수집을 한 번 실행한다.
- 기업마당 공고 ID를 기본 식별자로 사용하고 없을 때 원문 URL을 사용한다. 제목 유사도는 중복 키가 아니다.

공식 목록 endpoint는 `GET https://www.bizinfo.go.kr/uss/rss/bizinfoApi.do`다. `crtfcKey`, `dataType=json`, `pageUnit`, `pageIndex`를 사용하고 자격증명은 `BIZINFO_API_KEY`에서 읽는다. 원본 응답은 변형하지 않고 저장한다.

API에 수정일 cursor가 없으므로 일일 수집은 최신순 페이지를 읽다가 마지막 성공 시점보다 오래됐고 모든 항목의 해시가 같은 페이지가 연속 두 개 나오면 멈춘다. 주간 전체 대조는 모든 페이지를 받은 성공 snapshot만 유효하다. 두 번 연속 성공한 전체 snapshot에서 모두 빠진 공고만 `원문 확인 불가`로 바꾼다.

공고의 API 원본, 본문과 첨부 해시가 모두 같으면 새 버전과 분석을 만들지 않는다. 변경되면 새 공고버전을 만들고 재분석한다. 재분석 중에는 이전 판정을 `변경 전 결과`로 표시하며 최종 실패하면 현재 버전을 NEEDS_CONFIRMATION으로 공개하고 이전 판정은 접힌 이력으로 유지한다.

시연 fixture는 운영 parser를 통과한 실제 응답 wrapper를 사용한다. manifest에는 공고 ID, 공고버전 해시와 원본·첨부 해시를 기록한다. 시연 모드에서 네트워크 결과는 fixture를 덮어쓰지 못한다.

## 모집 기간

시작·종료일은 Asia/Seoul 날짜로 해석하며 마감 당일을 모집 중에 포함한다. 기간을 해석하지 못한 공고도 노출하고 `기간 확인 필요`로 표시한다. 마감 공고는 판정을 유지한 채 전체 공고의 모집 상태 필터에서 조회한다. 정상 카드에는 별도 모집 상태 배지를 추가하지 않는다.

## 첨부파일

모든 첨부의 파일명, URL, 공개 크기와 순서를 먼저 저장한다. 원본 바이너리는 파일별 20MB, 공고별 합계 100MB 한도 안에서만 로컬 파일시스템에 보관한다. 경로·해시·MIME·크기·버전과 [파일 상태](02-data-and-api.md#파일-상태)는 PostgreSQL에 저장한다.

PDF, HWP/HWPX, DOCX, XLSX, 이미지와 ZIP을 지원한다. ZIP은 한 단계만 해제하며 최대 200개 항목, 항목별 20MB, 공고 전체 100MB 제한을 동일하게 적용한다. 심볼릭 링크, 절대경로, 상위경로, 장치파일과 실행 파일은 거부한다. 내부 ZIP은 저장만 하고 재귀 해제하지 않는다. 확장자와 MIME signature가 다르면 실행하지 않고 실패 사유를 기록한다.

20MB 초과 파일은 원본 대신 `LIMIT_EXCEEDED` 메타데이터를 남긴다. 총 한도에 도달하면 AI가 관련 가능성이 높다고 선택한 파일을 먼저 받고 나머지는 원문 순서를 따른다. 받지 못했거나 실패한 파일이 자격조건을 포함할 가능성이 있으면 공개 판정은 NEEDS_CONFIRMATION이다.

다운로드는 최초 시도 뒤 최대 두 번 추가 재시도한다. 손상, 암호화와 미지원 형식도 실패 사유를 보존한다. AI의 첨부 선택·제외 이유는 일반 로그가 아니라 `ai_stage_runs`의 구조화 출력으로 저장한다.

## 텍스트와 OCR

1. 형식별 네이티브 텍스트를 먼저 추출한다.
2. 텍스트를 추출하지 못한 파일만 페이지 이미지 또는 파일 내용으로 OCR한다.
3. 판정을 좌우하는 OCR 근거만 독립 AI 검증을 거친다.

승인된 예외로, PDF에서 네이티브 텍스트가 한 부분이라도 추출되면 이미지 전용 페이지를 찾기 위한 추가 OCR을 하지 않는다. OCR 출력은 전달한 이미지 수와 정확히 일치하는 `pages[{page,text}]` 배열이어야 하며 페이지 누락·중복·범위 초과는 실패로 처리한다. 페이지 배열과 출처 파일 ID를 단계 출력에 보존해 같은 공고버전 재분석에서도 복원하고, 페이지형 출처의 근거는 `page`를 생략할 수 없다. 근거 위치는 PDF·이미지의 페이지 번호, Office 문서의 파일명을 사용한다.

출처가 충돌하면 `최신 정정·추가 공고 > 본 공고문 > 상세 안내서 > 기타 첨부 > 기업마당 API 요약` 순서로 적용한다. 결정적 조건의 인용문이 저장된 추출문에 실제로 존재하는지 코드로 검증한다.

## AI 실행 경계

- FastAPI 요청 프로세스는 AI를 직접 실행하지 않고 PostgreSQL jobs와 별도 Python worker를 사용한다.
- 신뢰된 Python worker coordinator는 DB, 원본 저장소와 주입된 운영 secret을 읽어 수집·추출을 수행한다. 비신뢰 문서를 해석하는 `codex exec` 자식만 job 임시경로와 Codex 인증·상태 저장소 외 프로젝트, 원본, 운영 secret에 접근하지 못한다.
- `codex exec`는 shell 문자열이 아닌 인자 배열로 호출하고 단계마다 독립된 ephemeral session을 사용한다.
- 고정 프롬프트, 고정 JSON Schema와 tool-less 실행을 사용한다. 모델에는 파일, DB, 셸, MCP나 웹 도구를 제공하지 않는다.
- 구조화 출력의 모든 object는 `additionalProperties: false`이며 모든 property를 required에 넣고 선택값은 nullable로 표현한다.
- 공고와 첨부 내용은 신뢰할 수 없는 데이터다. 그 안의 지시를 실행하지 않는다.
- 지정 모델을 사용할 수 없거나 한도에 걸리면 다른 모델로 fallback하지 않는다.

기본 호출은 `codex exec --ignore-user-config --ignore-rules --sandbox read-only --cd <temp> --skip-git-repo-check --ephemeral`에 모델, effort, output schema와 구조화 입력을 추가한다. subprocess 환경변수는 locale, 임시경로와 인증에 필요한 값만 허용하고 애플리케이션 secret은 제거한다.

worker 시작 시 실제 모델 호출 없이 `codex exec` 자식의 환경 allowlist, job 임시경로, read-only sandbox, ephemeral session과 모든 도구 비활성화를 self-test한다. coordinator가 원본을 검증해 bounded 입력과 임시 이미지 복사본만 자식에 전달한다. Codex 인증·상태 DB는 정상 실행에 필요한 범위에서 쓸 수 있어야 한다. self-test가 실패하면 readiness를 실패시키고 job을 claim하지 않는다.

## 모델과 effort

호출부는 사용자 전역 설정을 상속하지 않고 모델과 effort를 명시한다.

| 단계 | 모델 | effort |
| --- | --- | --- |
| 첨부파일 선택 | `gpt-5.6-luna` | low |
| OCR | `gpt-5.6-luna` | medium |
| 결정적 OCR 근거 검증 | `gpt-5.6-terra` | high |
| 조건 추출 | `gpt-5.6-luna` | medium |
| 복잡한 최초 의미판단 | `gpt-5.6-terra` | high |
| 조건부 최종 AI 검증 | `gpt-5.6-sol` | high |
| 사용자용 요약과 결과 설명 | `gpt-5.6-luna` | medium |

Sol은 AI가 직접 판단한 복잡한 자격조건 또는 판정을 좌우하는 OCR 근거가 있을 때만 실행한다. 첫 의미판단과 검증이 충돌하면 Sol 결과를 채택하지만 Python의 확정 비교가 항상 우선한다. 상세 이유는 [D-05](07-decisions.md#d-05-ai-모델-배치)에 기록한다.

## 모델 입력과 기록

기업정보 전체를 AI에 보내는 것은 승인된 제품 경계다. 항상 판정에 실제 사용한 불변 기업정보버전을 전송하고, 전송 범위는 해당 사용자와 대상 공고로 제한한다.

한 AI 요청의 정규화된 텍스트 입력은 최대 180,000자다. 첨부 텍스트가 이를 넘으면 관련성이 높은 핵심 자료와 짧은 파일을 우선하고 `ATTACHMENT_INPUT_TRUNCATED`를 기록한다. 잘린 입력으로 만든 분석은 자동 확정 판정을 공개하지 않고 NEEDS_CONFIRMATION으로 내린다.

DB에는 구조화 출력, 모델, effort, 프롬프트·스키마 버전, 입력 공고버전, 첨부 해시, 기업정보버전, 근거, 처리 시간, 재시도와 오류 코드를 저장한다. 의미판단에는 해당 condition의 최신 사용자 답변을 데이터로 포함하고 답변 fingerprint가 정확히 일치하는 단계 결과만 재사용한다. 답변이 달라지면 기존 판정을 유지한 채 해당 기업정보버전만 재분석하며, boolean 답변을 곧바로 PASS·FAIL로 변환하지 않는다. 전체 원시 응답은 저장하지 않는다. 일반 로그에는 기업정보 원문 대신 ID와 버전 ID만 남긴다.

## Canonical IR

조건 추출 결과는 다음 구조를 사용한다.

- `analysis_version`, `summary`, `tracks[]`, `roles[]`
- `groups[]`: `group_id`, `parent_group_id`, `operator(ALL|ANY)`, 적용 track과 role
- `conditions[]`: `condition_id`, `group_id`, `kind(MANDATORY|PREFERENCE|GUIDANCE|POST_AWARD)`, `subject`, `operator`, `expected_value`, `unit`, `reference_date`, `evidence[]`
- `evidence[]`: `source_file_id`, `source_version`, `page`, `verbatim_text`, `source_priority`
- `questions[]`: UNKNOWN을 해소할 질문, 답변 자료형과 condition ID

`subject`는 `BUSINESS_ENTITY_TYPE | ORGANIZATION_TYPE | COMPANY_SCALE | FOUNDED_ON | ELIGIBLE_REGION | PRIMARY_INDUSTRY | SECONDARY_INDUSTRY | ANNUAL_REVENUE | EMPLOYEE_COUNT | DELINQUENCY_STATUS | CERTIFICATION | SUPPORT_HISTORY | CAPABILITY_TAG | OTHER`의 폐쇄 enum이다. `operator`는 `EQ | NE | IN | NOT_IN | LT | LTE | GT | GTE | BETWEEN | CONTAINS | NOT_CONTAINS | EXISTS | SEMANTIC_MATCH`다. `expected_value`는 `STRING | INTEGER | DATE | ENUM | REGION_SET | STRING_SET | RANGE | BOOLEAN` 타입 태그와 해당 값을 가진 union이며 비교 불가능한 `OTHER`는 nullable 값과 원문 근거를 남긴다.

subject·operator·expected type·unit도 폐쇄 조합이다. enum 필드는 `EQ/NE + ENUM` 또는 `IN/NOT_IN + STRING_SET`, 날짜는 날짜 비교와 `DATE`, 지역은 `IN/NOT_IN + REGION_SET`, 매출·인원은 수치 비교와 `INTEGER` 또는 `BETWEEN + RANGE`만 허용한다. 매출 단위는 `원`, 인원 단위는 `명`이어야 하고 다른 필드는 단위를 갖지 않는다. 문자열·문자열 집합 필드는 선언된 동등·포함·집합·의미 비교만 허용한다. `EXISTS`는 expected value와 unit 없이 사용하고 `OTHER`는 `SEMANTIC_MATCH`만 허용한다. 이 조합을 벗어난 모델 출력은 판정 전에 schema 오류로 종료해 자동 `INELIGIBLE` 근거로 사용하지 않는다.

프롬프트는 문서 내용을 data delimiter 안에 넣고 IR 생성 외 행동을 요구하지 않는다. prompt와 schema는 독립 버전 파일로 관리하며 입력·프롬프트·스키마·모델·effort가 idempotency hash에 포함된다.

Terra OCR 검증은 OCR 문자열과 원본 페이지의 일치만 수정할 수 있다. Terra 의미판단은 지정 condition의 PASS·UNKNOWN·FAIL 후보와 근거만 반환한다. Sol 검증은 `ACCEPT | CORRECT | UNRESOLVED`를 반환한다. CORRECT는 새 stage output으로 남기고 UNRESOLVED는 UNKNOWN으로 집계한다. 검증 결과도 같은 근거·대상·단위 검사를 다시 통과해야 한다.

## 작업 큐

- `FOR UPDATE SKIP LOCKED`로 claim하고 전역 동시성은 최대 5다.
- 병렬 job은 독립 DB session과 transaction을 사용한다.
- 상태는 `QUEUED → RUNNING → SUCCEEDED | FAILED_RETRYABLE | FAILED_FINAL`이다.
- heartbeat는 30초, lease는 기본 15분이다. lease를 잃은 worker는 결과를 commit하거나 공개하지 못한다.
- 단계 timeout은 기본 300초다. 최초 실패 뒤 5초, 20초 backoff로 최대 두 번 추가 시도한다.
- 네트워크·한도 오류와 schema·입력 오류를 재시도 가능 여부가 다른 오류 코드로 분리한다.
- parent 작업을 중단하면 같은 process group의 Codex 자식을 종료한다. 종료와 lease 상실을 확인한 뒤에만 중단 완료로 보고한다.
- 공개 포인터 교체와 SUCCEEDED 기록은 같은 transaction에서 수행한다.

## 처리와 공개

기본 흐름은 다음과 같다.

`수집 → 원본·첨부 메타데이터 저장 → 첨부 선택 → 텍스트 추출 → 필요 시 OCR → 필요 시 OCR 검증 → 조건 추출 → 코드 비교·AI 의미판단 → 조건 집계 → 필요 시 Sol 검증 → 요약·설명 → 원자적 공개`

필수 단계 전에는 임시 판정을 공개하지 않는다. 설명 모델은 `calculated_verdict`를 변경할 수 없다. 설명 생성이 최종 실패하면 계산값을 보존하고 `published_verdict=NEEDS_CONFIRMATION`, `decision_origin=SYSTEM_FAILURE`로 공개하며 고정 문구 `결과 설명 생성에 실패해 원문 확인이 필요합니다.`를 제공한다.

작업 대기·실행 중인 최초 공고는 목록에서 숨긴다. 최종 실패 공고는 내부 오류와 실패 job을 보존하고 사용자에게 NEEDS_CONFIRMATION으로 공개한다.
