# 실제 서비스 통합 E2E

기본 `npm run test:e2e`는 빠른 mock 브라우저 검증만 실행한다. 다음 테스트는 별도의 임시 PostgreSQL과 실제 FastAPI를 대상으로 명시적으로 opt-in할 때만 실행한다.

필수 준비:

- PostgreSQL 시작과 `alembic upgrade head`
- `127.0.0.1:8000`에서 FastAPI 실행
- API의 `APP_ORIGIN`을 `E2E_BASE_URL`과 동일하게 설정
- fixture 원본을 `SOURCE_STORAGE_ROOT` 아래에 보관할 수 있는 쓰기 권한
- Chrome 설치

`E2E_START_BACKEND=1`을 추가하면 Playwright가 FastAPI도 시작한다. 이 경우 CI job에는 PostgreSQL service, `DATABASE_URL`, migration과 Python/Node dependency 설치만 준비하면 된다. 이미 실행 중인 API를 사용할 때는 이 변수를 생략한다.

```bash
cd frontend
INTEGRATION_E2E=1 \
E2E_USERNAME=e2e.user \
E2E_PASSWORD='<test-only password>' \
E2E_BASE_URL=http://localhost:4173 \
E2E_START_BACKEND=1 \
npm run test:e2e:integration
```

global setup은 지정 계정만 생성하거나 비밀번호를 초기화하고, 실제 session·CSRF·ETag 요청으로 합성 기업정보를 저장한 뒤 `fixtures/demo/manifest.json` 한 건만 적재한다. 테스트는 로그인, 기업정보 저장, 공고 상세 근거, 관심 상태와 인증된 fixture 다운로드를 검증한다. 운영 DB나 실제 기업정보를 대상으로 실행하지 않는다.
