# 자취 식탁 — 오늘 뭐 사 먹지?

자취생을 위한 장보기 추천 웹앱. 지금 **평소보다 싼** 식재료 중에서 **1인이 안 버리고 잘 먹을 수 있는**(보관·소분 쉬운) 재료를 우선으로 보여주고, 제철이면 가산점을 줍니다. 재료를 누르면 레시피 검색 결과(만개의레시피·네이버 블로그·유튜브)로 바로 연결됩니다.

가격 데이터는 KAMIS(한국농수산식품유통공사) 농산물유통정보 Open API를 사용하며, **API 키가 하나도 없어도 샘플 데이터로 그대로 동작**합니다(graceful degradation).

## 로컬 실행

Python 3.11 환경에서, 프로젝트 루트(이 README가 있는 폴더)에서:

```
pip install -r requirements.txt
python run.py
```

브라우저에서 http://127.0.0.1:8000 접속. (`run.py`는 `.env`를 로드한 뒤 `127.0.0.1:8000`으로 uvicorn을 띄웁니다. `.env`가 없어도 에러 없이 샘플 모드로 동작합니다.)

키를 설정하지 않으면 화면 상단에 "샘플 데이터 (KAMIS 키 연결 전)" 배지가 보이며, 번들된 샘플 시세로 추천이 동작합니다.

## PaaS 배포 (Render / Railway / Cloudtype — 호스트 비종속)

이 저장소는 특정 호스트에 묶이지 않습니다. Render·Railway·Cloudtype 모두 동일하게:

1. 저장소를 호스트에 연결한다.
2. **Build command**: `pip install -r requirements.txt`
3. **Start command**: 루트의 `Procfile`에서 자동 인식됩니다. 호스트가 Procfile을 읽지 않으면 시작 명령을 직접 지정:
   ```
   uvicorn backend.main:app --host 0.0.0.0 --port $PORT
   ```
   (`0.0.0.0` 바인딩 + 호스트가 주입하는 `$PORT`가 핵심입니다. 코드 변경 불필요 — uvicorn CLI가 호스트/포트를 처리합니다.)
4. Python 버전은 `runtime.txt`(`python-3.11.9`)로 고정됩니다.
5. **환경 변수**는 호스트 대시보드에 설정합니다(아래 표). `.env` 파일은 커밋되지 않으므로 배포 환경에선 대시보드 변수만 사용됩니다.

헬스 체크 경로: `GET /healthz` → `200 {"ok": true}` (외부 호출·의존성 없음). 호스트의 health probe에 이 경로를 지정하세요.

### 환경 변수

| 변수 | 필수 여부 | 없을 때 동작 | 발급처 |
|---|---|---|---|
| `KAMIS_CERT_KEY`, `KAMIS_CERT_ID` | 실시간 시세에 필요 | 샘플 시세로 폴백 — 앱은 정상 동작 | kamis.or.kr → 고객센터 → Open-API 신청 |
| `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET` | 선택 | 네이버 블로그 결과가 외부 검색 링크로 폴백 | developers.naver.com → 검색 API |
| `YOUTUBE_API_KEY` | 선택 | 유튜브 결과가 외부 검색 링크로 폴백 | Google Cloud Console → YouTube Data API v3 |

만개의레시피 인라인 결과는 **키 없이도** 동작합니다. 위 키들은 전부 선택이며, 하나도 없어도 앱은 깨지지 않고 폴백합니다.

### 캐싱 / 쿼터

- KAMIS 시세는 30분 메모리 캐시.
- 레시피 결과는 쿼리당 약 12시간 캐시.

외부 API 호출이 사용자 수와 무관하게 유지되므로(사용자별 fan-out 없음) 쿼터 초과/차단 위험이 낮습니다.

## API

- `GET /api/recommendations?limit=12` — 추천 피드. `{ source, date, items[] }`
- `GET /api/ingredient/{name}` — 재료 단건 상세(메타 + 현재 시세 + 이유 + 레시피 링크). 모르는 재료면 404.
- `GET /api/combo-recipes?items=양파,두부` — 선택 재료 조합 레시피 링크.
- `GET /api/recipe-results?items=양파` — 인라인 레시피 검색 결과(3소스, 소스별 graceful 폴백, 항상 200).
- `GET /healthz` — 헬스 체크. `{ "ok": true }`
- `GET /` — 프론트엔드(`index.html`), 정적 자원은 `/static` 아래.

## 구조

```
jachwi-table/
  backend/
    main.py     FastAPI 라우트 + 정적 마운트 + /healthz
    kamis.py    KAMIS 어댑터 (실시간 우선 + 샘플 폴백, 파싱 격리)
    recipes.py  인라인 레시피 결과 (만개/네이버/유튜브, 소스별 폴백)
    ranking.py  추천 스코어링
    data/
      ingredients.json    큐레이션된 재료 메타(약 30종)
      sample_prices.json  폴백용 샘플 시세
      baseline.json       작년 평균 baseline (없거나 {} 이어도 무방, 추적됨)
  frontend/
    index.html / style.css / app.js   (빌드 없음, 순수 정적)
  scripts/
    build_baseline.py     baseline.json 생성 (유지보수용, 요청 경로와 무관)
    fixtures/             build_baseline.py가 쓰는 추적 데이터/픽스처
  run.py        로컬 실행 런처
  Procfile      PaaS 시작 명령
  runtime.txt   Python 버전 고정
```

## 유지보수: baseline.json 빌드

```
python scripts/build_baseline.py            # baseline.json 생성
python scripts/build_baseline.py --selftest # 오프라인 파싱 자가검증만
```

`build_baseline.py`는 KAMIS monthlySalesList로 작년(직전 완결 연도) 평균을 산출해 `backend/data/baseline.json`에 씁니다. 웹앱이 요청 처리 중에 호출하는 일은 절대 없으니 **요청 단위가 아니라 스케줄(예: 연 1회) 잡으로** 돌리세요.

**알려진 한계(버그 아님)**: KAMIS monthlySalesList는 소매(retail)를 요청해도 도매(wholesale)만 반환하는 경우가 있어, `baseline.json`이 비어 있는 채로 남을 수 있습니다. 이때 앱은 `kamis.py`의 median-of-3 폴백(1일전·1개월전·1년전 중앙값)으로 "평소 대비"를 계산합니다. 이는 의도된 동작입니다.

## Git 초기화 / 배포

이 저장소는 아직 git으로 초기화되어 있지 않습니다. 배포 전:

```
git init
git add .
git commit -m "Prepare for PaaS deployment"
```

이후 PaaS에 연결된 원격 저장소로 push 하세요. `.env`(시크릿)는 `.gitignore`에 있어 커밋되지 않습니다 — 키는 호스트 대시보드 환경 변수로 설정합니다.

## 메모

KAMIS 응답 JSON 형태는 실제로 일관적이지 않아(필드명·`data`가 객체/배열 혼재, "3,500"·"-" 등) 모든 KAMIS 파싱은 `backend/kamis.py` 안에 `PriceRow` 계약 뒤로 격리되어 있습니다. 실제 키로 튜닝이 필요하면 `kamis.py` 상단의 필드명 가정 주석 블록만 조정하면 되고 `ranking.py`/`main.py`는 건드릴 필요가 없습니다.
