# 운영 계약과 runbook

이 문서는 기술 스택, 환경설정, CLI, 프로세스 수명주기, 배포와 복구를 소유한다. 변경 후 필요한 검증은 [06-acceptance.md](06-acceptance.md)를 따른다.

## 기술 스택과 저장소 구조

백엔드는 Python, uv lockfile, FastAPI, Pydantic v2, SQLAlchemy 2 async, Alembic와 pytest를 사용한다. 프런트엔드는 React, Vite, TypeScript strict, Tailwind CSS, shadcn/ui, Vitest, Testing Library와 Playwright를 사용한다. DB는 Docker Compose PostgreSQL이다. scaffold 시점에 지원되는 LTS runtime을 선택해 버전과 lockfile을 커밋한다.

```text
backend/
  pyproject.toml
  src/app/
  migrations/
  tests/
frontend/
  package.json
  src/
  tests/
data/
fixtures/
  demo/manifest.json
infra/
  cloudflared/
docs/
docker-compose.yml
.env.example
```

원본 첨부, 실제 `.env`, 세션·인증 파일, AI 임시파일은 git에서 제외한다.

## 환경변수

`.env.example`에는 secret 값을 넣지 않고 다음 이름과 설명을 둔다.

- `DATABASE_URL`
- `APP_ORIGIN`
- `SESSION_SECRET`
- `BIZINFO_API_KEY`
- `SOURCE_STORAGE_ROOT`
- `DEMO_FIXTURE_ROOT`
- `APP_TIMEZONE=Asia/Seoul`
- `AI_MAX_CONCURRENCY=5`
- `AI_STAGE_TIMEOUT_SECONDS=300`
- `VITE_API_BASE=/api/v1`
- `CLOUDFLARE_TUNNEL_CONFIG`

시작 시 필수값, 저장경로 권한, URL 조합과 AI 격리 self-test를 검증한다. secret 값은 로그와 readiness 응답에 남기지 않는다.

## 배포 구조

- 프런트엔드: Vercel의 `frontend` root
- API: 이 Mac의 FastAPI
- DB: 이 Mac의 Docker Compose PostgreSQL
- 비동기 작업: PostgreSQL jobs와 별도 Python worker. Redis는 사용하지 않음
- 공개 연결: Cloudflare Tunnel
- 운영·readiness: Cloudflare Access로 보호
- 서비스 프로세스: 관리자가 직접 시작·종료하며 자동 시작하지 않음

브라우저는 상대 경로 `/api/v1`만 사용하고 Vercel rewrite가 Tunnel로 전달한다. 브라우저 코드에 backend hostname을 넣지 않는다. Tunnel은 네트워크 경로일 뿐 사용자 인증을 대신하지 않는다.

프런트 Vercel 빌드는 저장소 밖 파일이나 상위 `spec-pack`에 의존하지 않는다. 지역 데이터 등 runtime asset은 [저장소 내부 canonical 계약](02-data-and-api.md#지역-데이터)에 따라 배포 사본을 만든다.

개인정보 처리방침에는 저장된 기업정보 전체와 공고 내용이 AI 처리를 위해 Codex로 전송된다는 사실을 명시한다. 별도 동의 팝업은 만들지 않는다.

## Preflight

운영 시작 전에 다음을 확인한다.

1. `.env` 필수값과 저장경로 권한
2. Docker와 PostgreSQL 연결
3. Alembic 현재 revision과 pending migration
4. Codex OAuth 로그인, CLI 버전과 인증·상태 저장소 쓰기
5. worker 격리 self-test와 잔존 Codex 자식 프로세스
6. Cloudflare 설정 파일 권한과 hostname
7. 프런트 rewrite 대상과 공개 origin

검사 실패 항목이 있으면 readiness를 실패시키고 worker job을 시작하지 않는다.

## 시작과 종료

구현할 표준 명령은 다음 형태로 고정한다.

```bash
docker compose up -d db
cd backend && uv sync
cd backend && uv run alembic upgrade head
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd backend && uv run python -m app.worker
cloudflared tunnel --config infra/cloudflared/config.yml run
```

관리자가 유지하는 별도 터미널 세션에서 실행한다. Codex 임시 실행 세션에 장기 프로세스를 종속시키지 않는다. 종료는 Tunnel, worker, FastAPI, PostgreSQL 순서다. worker 종료 시 claim 중단, 자식 process group 종료, lease·DB 상태 확인 뒤 종료 완료를 보고한다.

## 관리 CLI

하나의 `cd backend && uv run python -m app.cli` 진입점 아래 다음 하위 명령을 제공한다.

- `user create`, `user reset-password`
- `collect run`, `collect reconcile`
- `announcement analyze --announcement-id <id>`
- `announcement reanalyze --announcement-id <id>`
- `decision reevaluate --announcement-id <id>`
- `job status`, `job retry --job-id <id>`
- `fixture load --manifest <path>`
- `acceptance holdout --manifest <path>`

대상 변경 명령은 먼저 ID, 버전과 예상 건수를 표시한다. 대량 작업은 명시적 `--all` 없이는 실행하지 않는다. 실제 기업정보가 AI로 전송되는 명령은 대상 사용자·공고 수, 전송 필드, 모델과 effort를 먼저 보여 주고 확인받는다. 처리 건수 0을 성공적인 수집·분석·재판정으로 보고하지 않는다.

원본·버전 삭제와 DB volume 초기화 명령은 MVP CLI에 추가하지 않는다.

## Health

`GET /api/v1/health/live`는 프로세스 생존만 `{"status":"ok"}`로 반환하며 DB, 경로, 버전, 오류와 모델 정보를 노출하지 않는다.

Cloudflare Access로 보호한 `GET /api/v1/ops/health/ready`는 DB, worker heartbeat, 저장공간, 기업마당 credential 존재 여부, Codex 로그인과 격리 self-test를 항목별로 반환한다. secret 값과 로컬 절대경로는 반환하지 않는다.

## 복구 순서

장애는 다음 순서로 범위를 좁힌다.

1. 소유 프로세스와 포트, 잔존 자식 프로세스를 확인한다.
2. DB, FastAPI, worker heartbeat를 각각 확인한다.
3. 로컬 liveness와 보호 readiness를 구분해 확인한다.
4. Tunnel 연결과 Cloudflare Access 정책을 확인한다.
5. Vercel rewrite와 공개 liveness를 확인한다.
6. 로그인부터 공고 상세·첨부 다운로드까지 사용자 흐름을 확인한다.

수집 성공, 분석 성공, 판정 성공은 서로 다른 상태다. 기업정보나 분석을 새로 저장했다고 과거 판정이 최신이라고 가정하지 않고 입력 버전과 완료 시각을 확인한다. 로컬 빌드 성공도 배포 성공을 의미하지 않는다.

샌드박스에서 uv cache 권한이 막히면 `UV_CACHE_DIR=/tmp/solverthon-uv-cache`를 사용한다.

## 기술 근거

제품 결정은 저장소 문서를 따르되 구현 도구의 현재 동작은 공식 문서에서 다시 확인한다.

- [Codex 인증](https://learn.chatgpt.com/docs/auth)
- [Codex 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Codex 모델](https://learn.chatgpt.com/docs/models)
- [Vercel rewrites](https://vercel.com/docs/routing/rewrites)
- [Cloudflare Access application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/choose-application-type/)
- [기업마당 지원사업정보 API](https://www.bizinfo.go.kr/apiDetail.do?id=bizinfoApi)
