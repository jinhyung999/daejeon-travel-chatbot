# 소품샵 신규 수집 및 추천 장소 정보 보강 설계

## 목표

대전 소품샵을 네이버 지역검색으로 100여 건 신규 수집해 `place`에 추가하고, `recommend='추천'`으로 표시된 모든 장소(현재 음식점 696건 + 신규 소품샵)를 대상으로 블로그 검색 기반 정보 보강을 수행한다. 보강 대상은 카테고리에 무관하게 `recommend='추천'` 조건 하나로 결정되므로, 이후 카페 등이 같은 방식으로 추천 표시되면 별도 작업 없이 자동으로 보강 대상에 포함된다.

## 배경

- `place.recommend`는 [[2026-07-21-restaurant-recommend-db-design]] 문서에서 이미 도입되었고, 현재 `추천` 표시된 행은 696건(전부 restaurant, 출처는 tourapi 169 / sbiz 325 / naver_search 108 / daejeon_food 94)이다.
- 이 696건조차 `open_time` 16%, `close_day` 17%, `has_parking` 0%만 채워져 있어 보강 여지가 크다.
- `collectors/detail_enrich.py`가 이미 "조건에 맞는 `place` 행 조회 → 외부 API 호출 → 같은 행 UPDATE" 패턴의 보강 수집기로 존재한다. 이번 보강기도 같은 패턴을 따른다.
- 대전 소상공인시장진흥공단 CSV(액세서리/잡화·기념품점·예술품 소매업)를 검토했으나 유통업체·경매장 등 노이즈가 섞여있어 채택하지 않았다. 네이버 지역검색은 `가구,인테리어>인테리어소품`이라는 정확한 카테고리를 제공하며 실제 조회 결과도 노이즈 없이 깨끗했다.
- 네이버 블로그 검색 API는 `title/link/description/bloggername/bloggerlink/postdate` 6개 필드만 제공하며 본문 전체는 제공하지 않는다. `description`(검색어 주변 발췌 스니펫)만으로도 영업시간·휴무일·주차·컨셉을 유추할 수 있는 사례를 실제로 확인했다.
- 네이버 오픈API 이용약관상 블로그 제목·요약·본문의 저장 가능 여부를 확인하지 못했다(`developers.naver.com` 접근이 도구 정책상 차단됨). 확인 전까지는 보수적으로 원문을 저장하지 않는다.

## 선택한 접근법

수집(네이버 API)과 보강(LLM 가공)을 분리하되, 원본 블로그 스니펫은 같은 스크립트 실행 안에서만 메모리로 다루고 디스크에는 남기지 않는다. 스니펫을 먼저 저장해두고 나중에 가공하는 방식은 엔지니어링상 유연하지만, 블로그 본문/요약을 축적하지 않는다는 기존 원칙([[2026-07-20-naver-restaurant-candidates-design]])과 충돌하므로 채택하지 않는다.

6개 보강 필드 추출은 OpenAI API로 자동화한다. 목표 수량이 소품샵 100여 건, 이후 restaurant/cafe도 각 수백 건 규모로 반복 적용할 계획이라 수동 처리는 재실행성과 작업량 면에서 맞지 않는다.

`concept_tag`는 외래키나 다대다 테이블로 정규화하지 않고 단순 TEXT 컬럼으로 시작한다. 태그 표기가 흔들리는 문제는 있을 수 있으나, 필요성이 확인되면 그때 정규화한다(YAGNI).

## 컴포넌트 1 — 소품샵 수집기 (`collectors/naver_giftshop.py`)

`tour_attraction.py`, `sbiz_market.py`와 동일하게 `common.py`의 `upsert_place`를 사용해 `place`에 직접 반영한다. 별도 검수 CSV 단계 없이 수집 즉시 `recommend='추천'`으로 반영한다.

- 검색: 네이버 지역검색 API, 구별 대표 동네 시드 × `"소품샵"` 키워드 조합 (예: 중구 대흥동/은행동, 서구 둔산동, 유성구 봉명동/관평동 등, [[2026-07-20-naver-restaurant-candidates-design]]의 위치 시드 목록을 재사용)
- 목표 수량: 100여 건. 조합을 다 돌아도 미달이면 있는 만큼만 반영하고 부족분을 종료 요약에 명시한다(임의로 채우지 않는다).
- 중복 제거: `sbiz_market.py`의 이름 정규화 + 좌표 반경 30m 판정 로직을 재사용해 기존 `place`와도, 같은 실행 내 후보끼리도 중복을 걸러낸다.
- 필드 매핑:
  - `place_id`: 상호명 + 도로명 주소 + 좌표 기반 결정적 해시 ID ([[2026-07-21-restaurant-recommend-db-design]]과 동일한 방식, 재실행해도 동일 ID로 중복 삽입 방지)
  - `name`, `address`: 지역검색 응답 그대로
  - `category`: `giftshop` (신규 값)
  - `lat`, `lng`: `mapx`/`mapy`를 10,000,000으로 나눠 변환
  - `tel`: 지역검색 `telephone`
  - `source_api`: `naver_search`
  - `recommend`: `추천`
  - `extra_json`: `{"naver_category": "가구,인테리어>인테리어소품", "naver_link": "..."}"`
  - `homepage`, `open_time`, `close_day`, `fee`, `has_parking`: NULL (컴포넌트 2 보강기가 채움)

## 컴포넌트 2 — 추천 장소 정보 보강기 (`collectors/blog_concept_enrich.py`)

`detail_enrich.py`와 동일한 구조: 대상 조회 → 외부 API 호출 → 같은 행 UPDATE, 20건마다 커밋.

### 처리 흐름

1. `SELECT place_id, name, address, category FROM place WHERE recommend='추천'` 로 대상 조회 (카테고리 무관)
2. 후보별로 네이버 블로그 검색을 두 번 호출해 스니펫을 모은다: 정확도순(sim) 최대 100건 + 날짜순(date) 최대 100건 (`naver_search.py`의 `search_blog` 재사용, 신규 API 호출 없음 — 기존 restaurant 수집기가 이미 같은 목적으로 호출하던 것과 동일 패턴)
3. 모은 스니펫(`description` 필드들)을 하나로 묶어 OpenAI API에 1회 전달해 아래 6개 필드를 추출한다. 스니펫 원문은 이 처리가 끝나면 메모리에서 버리고 디스크에 저장하지 않는다.
4. 결과를 같은 `place` 행에 UPDATE:
   - `open_time`, `close_day`, `has_parking`: 기존 값이 있으면 유지, NULL일 때만 채움 (`COALESCE`) — TourAPI 확정 정보를 블로그 추정치가 덮어쓰지 않도록
   - `concept_tag`, `photo_spot`, `has_workshop`, `blog_url_1`, `blog_url_2`, `blog_url_3`: 신규 컬럼에 직접 SET

### 6개 보강 필드

| 필드 | 값 | 비고 |
|---|---|---|
| `concept_tag` | TEXT, 자유 텍스트 (예: 빈티지, 문구, 캐릭터굿즈, 도자기공방) | 정규화하지 않음 |
| `open_time_guess` → `open_time` | 기존 컬럼에 병합 | NULL일 때만 |
| `close_day_guess` → `close_day` | 기존 컬럼에 병합 | NULL일 때만 |
| `parking_guess` → `has_parking` | 0/1/NULL | 기존 컬럼에 병합, NULL일 때만 |
| `photo_spot` | INTEGER 0/1/NULL | 포토존/인스타 감성 여부 |
| `has_workshop` | INTEGER 0/1/NULL | 원데이클래스·체험 결합 여부 |

스니펫에 근거가 없는 필드는 NULL로 남긴다(임의 추정 금지).

## 스키마 변경

```sql
ALTER TABLE place ADD COLUMN concept_tag TEXT;
ALTER TABLE place ADD COLUMN photo_spot INTEGER;
ALTER TABLE place ADD COLUMN has_workshop INTEGER;
ALTER TABLE place ADD COLUMN blog_url_1 TEXT;
ALTER TABLE place ADD COLUMN blog_url_2 TEXT;
ALTER TABLE place ADD COLUMN blog_url_3 TEXT;
```

`db/schema.sql`(신규 DB용)과 기존 `travel.db` 마이그레이션 양쪽에 반영한다. `category='giftshop'`은 기존 `idx_place_category` 인덱스를 그대로 사용하므로 추가 인덱스는 불필요하다.

## 응답 시점(챗봇) 연동 — 설계만, 이번 범위 아님

`app/main.py`가 비어있어 챗봇 응답 로직 자체가 아직 없다. 향후 구현 시 다음 순서를 따르도록 설계만 남겨둔다: 보강된 6필드로 답변이 충분하면 그대로 사용하고, 사용자가 추가 설명을 요구하거나 필드가 NULL이라 답을 못 채우는 경우에만 `blog_url_1~3` 중 하나를 열어 실시간으로 보충한다. 이 도구(URL 열람) 자체의 구현은 별도 설계가 필요하다.

## 정책 준수 및 미해결 이슈

- 블로그 제목/요약/본문은 CSV·DB 어디에도 저장하지 않는다. 저장하는 것은 LLM이 가공한 결과 필드와 링크(URL) 3개뿐이다.
- 네이버 오픈API 이용약관상 스니펫을 수집 스크립트 실행 중 일시적으로(메모리) 처리하는 것까지 제한하는지는 미확인 상태다. `developers.naver.com`이 도구 정책상 접근 차단되어 있어, 실제 운영 전 사용자가 직접 확인해야 한다.
- OpenAI API 연동은 이번에 신규로 추가한다 (`requirements.txt`에 `openai` 없음, `.env`의 `OPENAI_API_KEY`는 있으나 코드에서 미사용 상태).

## 안전성과 오류 처리

- 스키마 마이그레이션은 재실행 가능해야 한다 (컬럼이 이미 있으면 스킵).
- `place_id`는 결정적 해시이므로 소품샵 수집기를 재실행해도 중복 삽입되지 않는다.
- 네이버/OpenAI API 요청 실패(4xx)는 즉시 실패 처리, 429/5xx는 지수 백오프로 최대 3회 재시도한다 (`request_with_retry` 재사용).
- 보강기는 20건마다 커밋해 중간 실패 시에도 이미 처리한 내용을 보존한다 (`detail_enrich.py`와 동일).
- LLM 응답이 기대한 JSON 스키마가 아니면 해당 행은 스킵하고 실패 목록에 기록한다 (전체 실행을 중단하지 않음).

## 검증과 테스트

테스트는 구현보다 먼저 작성한다.

- 소품샵 수집기: 동일 입력 재실행 시 `place_id`가 같아 중복 삽입되지 않는다.
- 소품샵 수집기: 기존 `place`와 이름+좌표 30m 이내 중복은 스킵된다.
- 보강기: `recommend`가 `추천`이 아닌 행은 대상에서 제외된다.
- 보강기: `open_time`/`close_day`/`has_parking`은 기존 값이 있으면 덮어쓰지 않는다.
- 보강기: 스니펫이 하나도 없는 후보는 6필드 모두 NULL로 남기고 실패 처리하지 않는다.
- 보강기: 원본 스니펫 텍스트가 DB나 파일 어디에도 남지 않는다.
- 스키마 마이그레이션을 두 번 실행해도 컬럼이 중복 생성되지 않는다.
- `PRAGMA integrity_check` 결과가 `ok`여야 한다.

## 확장 계획 (이번 범위 밖)

- **restaurant**: 이미 `recommend='추천'` 696건이 있으므로 보강기만 실행하면 바로 적용됨.
- **cafe**: 현재 3,611건 전부 미검수. 소품샵과 달리 먼저 후보 검수 과정(승인 CSV)을 거쳐 `recommend='추천'`으로 표시한 뒤, 같은 보강기를 실행한다. 목표는 몇백 건 규모.

## 완료 기준

- `db/schema.sql`과 기존 DB 마이그레이션에 6개 컬럼이 반영된다.
- 소품샵이 실제 API로 100여 건(또는 조합 소진 시 그 이하) 수집되어 `place`에 `category='giftshop'`, `recommend='추천'`으로 반영된다.
- `recommend='추천'`인 모든 행(현재 696 + 신규 소품샵)에 대해 보강기 실행 결과가 기록된다 (성공/스킵/실패 건수 요약 포함).
- 원본 블로그 스니펫이 산출물이나 로그에 남지 않는다.
- 단위 테스트가 통과한다.
