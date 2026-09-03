# AI Solverthon 2026 · Imeditech

기업정보와 기업마당 지원사업 공고를 근거 단위로 비교해 `신청 가능`, `확인 필요`, `신청 어려움`을 보여 주는 MVP입니다. 판정 가능한 날짜·수치·enum·지역 규칙은 Python이 소유하고, AI는 문서 추출·의미 판단·설명만 담당합니다.

제품·API·AI·UI·운영 계약의 문서 권위와 변경 순서는 [docs/README.md](docs/README.md)에서 시작합니다.

## 구성

- `frontend`: React 19, Vite, TypeScript, Tailwind 기반 반응형 웹 UI
- `backend`: FastAPI, SQLAlchemy async, Alembic, Argon2id session 인증
- `backend/src/app/pipeline`: 기업마당 수집, 안전한 첨부 처리, Canonical IR, PostgreSQL job worker
- `data`: provenance와 SHA-256이 고정된 법정동 지역 데이터
- `fixtures/demo`: 외부 호출 없이 수집·분석·판정을 검증하는 1건 fixture
- `infra/cloudflared`: 비밀을 제외한 Tunnel 설정 템플릿

## 로컬 시작

필수 런타임은 Node 24와 Python 3.14입니다. `.env.example`을 루트 `.env`로 복사하고 로컬 값만 채웁니다. `.env`는 Git에서 제외됩니다.

```bash
docker compose up -d db
cd backend && UV_CACHE_DIR=/tmp/solverthon-uv-cache uv sync --locked
cd backend && UV_CACHE_DIR=/tmp/solverthon-uv-cache uv run alembic upgrade head
python3 scripts/preflight.py
```

API, worker, 프런트는 각각 별도 터미널에서 실행합니다.

```bash
cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
cd backend && uv run python -m app.worker
cd frontend && npm ci && npm run dev
```

worker는 `.env`, 문서, 원본 저장소를 읽을 수 없는 전용 OS 권한에서 격리 self-test를 통과해야 job을 가져옵니다. 검사 실패를 우회해 운영하지 않습니다.

## fixture와 관리 CLI

외부 기업마당 API나 AI 모델을 호출하지 않고 대표 공고를 적재할 수 있습니다.

```bash
cd backend
uv run python -m app.cli user create
uv run python -m app.cli fixture load --manifest ../fixtures/demo/manifest.json
uv run python -m app.cli job status --job-id <job-id>
```

같은 fixture를 다시 적재해 변경이 없으면 `processed=0`과 non-zero exit를 반환합니다. 실제 수집·분석·재판정 명령은 실행 전에 대상, 버전, 예상 건수와 모델·effort를 표시하며 처리 0건을 성공으로 보지 않습니다.

## 검증

```bash
cd backend && uv run ruff check src tests migrations
cd backend && uv run ruff format --check src tests migrations
cd backend && uv run pytest
cd frontend && npm run lint
cd frontend && npm run test -- --run
cd frontend && npm run build
cd frontend && npm run test:e2e
python3 scripts/check_region_sync.py
python3 scripts/check_tracked_secrets.py
git diff --check
```

홀드아웃 평가는 독립 작성된 5쌍 이상의 정답 manifest가 있어야 통과로 선언합니다.

## 배포 경로

- 웹: `https://ai-solverthon-2026-imt.party`
- API: `https://api.ai-solverthon-2026-imt.party`
- 브라우저는 상대 경로 `/api/v1`만 사용하고 Vercel rewrite가 API로 전달합니다.
- public liveness 외 `/api/v1/ops/*`는 Cloudflare Access로 보호합니다.

운영 시작·종료, readiness, 복구와 비밀 관리 절차는 [docs/05-operations.md](docs/05-operations.md)를 따릅니다.
