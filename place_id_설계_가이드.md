# `place_id` 설계 및 태그 테이블 연동 가이드

## 1. 문서 목적

이 문서는 현재 `place` 테이블의 `place_id`가 어떤 역할을 하는지 설명하고, 이를 `1, 2, 3, ...` 형태로 재정렬했을 때 발생하는 문제를 정리한다.

또한 태그 테이블을 추가할 때의 올바른 외래키 연결 방법과, 별도의 숫자 PK가 필요한 경우 사용할 수 있는 구조를 제안한다.

## 2. 현재 `place` 테이블 구조

현재 `place` 테이블에서는 `place_id`가 기본키(Primary Key, PK)다.

```sql
CREATE TABLE IF NOT EXISTS place (
  place_id    TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  category    TEXT NOT NULL,
  address     TEXT,
  lat         REAL,
  lng         REAL,
  source_api  TEXT
);
```

PK는 테이블의 각 행을 유일하게 구분하는 값이다. 따라서 동일한 `place_id`를 가진 장소를 두 번 저장할 수 없다.

현재 `place_id`는 단순한 화면 표시 순번이 아니라 다음 두 역할을 수행한다.

1. 데이터베이스에서 장소를 유일하게 구분한다.
2. 수집기 및 외부 API의 데이터와 장소를 연결한다.

## 3. 현재 `place_id` 생성 규칙

`place_id`는 모든 장소에 일괄적으로 숫자를 부여하는 방식이 아니다. 데이터 출처에 따라 다음과 같이 생성된다.

| 데이터 출처 | 생성 규칙 | 예시 |
|---|---|---|
| 한국관광공사 TourAPI | API의 `contentid` 사용 | `126508` |
| 대전 음식점 API | 이름과 주소의 SHA1 해시 | `daejeon_food_a81f03c29b17e430` |
| 대전 쇼핑 API | 이름과 주소의 SHA1 해시 | `daejeon_shopping_38c9...` |
| 네이버 기념품점 | 이름, 주소, 위도, 경도의 SHA256 해시 | `naver_giftshop_e8b1...` |
| 네이버 음식점 | 이름, 주소, 위도, 경도의 SHA256 해시 | `naver_restaurant_f29c...` |
| 소상공인 데이터 | 원본 상가업소번호에 접두어 추가 | `sbiz_12345678` |
| 행정안전부 숙박 데이터 | 원본 관리번호에 접두어 추가 | `mois_lodging_관리번호` |

이 규칙을 사용하면 같은 원본 데이터를 다시 수집했을 때 동일한 `place_id`가 생성된다. 수집기는 이를 이용해 신규 장소를 추가하거나 기존 장소를 갱신한다.

## 4. `place_id`를 재정렬하면 발생하는 문제

### 4.1 같은 장소가 중복 저장된다

현재 성심당의 ID가 다음과 같다고 가정한다.

```text
place_id                       | name
-------------------------------+------
daejeon_food_abc123            | 성심당
```

이를 임의로 `1`로 변경하면 다음과 같다.

```text
place_id | name
---------+------
1        | 성심당
```

하지만 다음 수집 시 음식점 수집기는 기존 생성 규칙에 따라 다시 `daejeon_food_abc123`을 만든다.

현재 저장 로직은 `place_id` 충돌 여부로 신규 데이터와 기존 데이터를 구분한다.

```sql
ON CONFLICT(place_id) DO UPDATE
```

`1`과 `daejeon_food_abc123`은 서로 다른 값이므로 수집기는 성심당을 신규 장소로 판단한다.

```text
place_id                       | name
-------------------------------+------
1                              | 성심당
daejeon_food_abc123            | 성심당
```

결과적으로 같은 장소가 중복 저장된다.

### 4.2 TourAPI 상세정보 조회가 실패할 수 있다

TourAPI에서 가져온 장소는 API의 `contentid`를 `place_id`로 사용한다.

```text
place_id = 126508
```

상세정보를 수집할 때도 이 값으로 API를 호출한다.

```text
contentId=126508
```

`place_id`를 임의로 `1`로 변경하면 API에 `contentId=1`을 전달하게 된다. 해당 ID가 존재하지 않거나 다른 콘텐츠를 가리킬 수 있으므로 홈페이지, 전화번호, 소개 등의 상세정보를 올바르게 가져오지 못한다.

### 4.3 태그와의 연결이 끊어질 수 있다

태그 연결 데이터가 다음과 같다고 가정한다.

```text
place
place_id                       | name
-------------------------------+------
daejeon_food_abc123            | 성심당

place_tag
place_id                       | tag_id
-------------------------------+-------
daejeon_food_abc123            | 1
```

`place.place_id`만 `1`로 바꾸면 `place_tag`는 여전히 이전 ID를 가리킨다.

```text
place
place_id | name
---------+------
1        | 성심당

place_tag
place_id                       | tag_id
-------------------------------+-------
daejeon_food_abc123            | 1
```

외래키 검사가 활성화되어 있으면 이런 변경이 거부될 수 있다. 외래키 검사가 비활성화되어 있으면 존재하지 않는 장소를 가리키는 잘못된 연결 데이터가 남을 수 있다.

### 4.4 추천·후보·좌표 데이터와의 연결이 끊어진다

일부 스크립트와 파일은 `place_id`를 저장한 뒤 이를 이용해 장소의 좌표나 상세정보를 다시 조회한다.

```sql
SELECT lat, lng
FROM place
WHERE place_id = 'daejeon_food_abc123';
```

DB의 ID가 `1`로 바뀌면 기존 ID로는 장소를 찾을 수 없다. 장소 자체가 삭제된 것은 아니지만 기존 추천 결과나 후보 데이터에서는 해당 장소를 연결할 수 없게 된다.

### 4.5 장소를 삭제할 때마다 다른 장소의 ID도 바뀐다

다음과 같이 번호를 부여했다고 가정한다.

```text
1 | 장소A
2 | 장소B
3 | 장소C
4 | 장소D
```

장소B를 삭제하면 번호에 빈자리가 생긴다.

```text
1 | 장소A
3 | 장소C
4 | 장소D
```

다시 연속 번호로 정렬하면 장소C와 장소D의 ID까지 바뀐다.

```text
1 | 장소A
2 | 장소C
3 | 장소D
```

장소 하나를 삭제했을 뿐인데 태그, 리뷰, 즐겨찾기, 추천 기록 등에서 장소C와 장소D를 가리키는 값도 모두 변경해야 한다.

PK는 순서를 표현하는 값이 아니므로 `1, 2, 5, 8`처럼 중간 번호가 비어 있어도 문제가 없다.

## 5. 태그 테이블 권장 설계

장소 하나에 여러 태그가 붙고, 하나의 태그가 여러 장소에 사용될 수 있으므로 `place`와 `tag` 사이에는 다대다 관계가 성립한다.

이를 표현하기 위해 `place_tag` 연결 테이블을 사용한다.

```sql
CREATE TABLE tag (
  tag_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  tag_name TEXT NOT NULL UNIQUE
);

CREATE TABLE place_tag (
  place_id TEXT NOT NULL,
  tag_id   INTEGER NOT NULL,

  PRIMARY KEY (place_id, tag_id),

  FOREIGN KEY (place_id)
    REFERENCES place(place_id)
    ON DELETE CASCADE,

  FOREIGN KEY (tag_id)
    REFERENCES tag(tag_id)
    ON DELETE CASCADE
);

CREATE INDEX idx_place_tag_tag_id
ON place_tag(tag_id);
```

이 설계는 기존 `place.place_id`의 PK를 옮기는 것이 아니다.

| 테이블 | PK | 설명 |
|---|---|---|
| `place` | `place_id` | 장소를 구분한다. |
| `tag` | `tag_id` | 태그를 구분한다. |
| `place_tag` | `(place_id, tag_id)` | 같은 장소와 태그의 중복 연결을 방지한다. |

`place_tag.place_id`는 `place.place_id`를 참조하는 외래키(Foreign Key, FK)이며, 동시에 `place_tag` 복합 PK의 일부다.

## 6. 화면에 연속 번호가 필요한 경우

화면이나 보고서에서 `1, 2, 3, ...` 순번이 필요한 경우 `place_id`를 변경하면 안 된다. 조회 결과에만 별도의 순번을 생성해야 한다.

```sql
SELECT
  ROW_NUMBER() OVER (ORDER BY name) AS row_no,
  place_id,
  name,
  category
FROM place;
```

예시 결과:

```text
row_no | place_id                       | name
-------+--------------------------------+----------
1      | daejeon_food_a81f03c29b17e430  | 성심당
2      | 126508                         | 한밭수목원
3      | sbiz_92837465                  | 카페A
```

- `row_no`: 화면에 보여 주는 임시 순번
- `place_id`: 장소 관계를 유지하는 고유 식별자

정렬 기준이 바뀌면 `row_no`는 달라질 수 있지만 `place_id`는 변하지 않는다.

## 7. 별도의 숫자 PK가 필요한 경우

DB 내부 관계에서 숫자 PK를 사용해야 하는 명확한 이유가 있다면 기존 `place_id`를 덮어쓰지 말고 새로운 컬럼을 추가한다.

권장 구조는 다음과 같다.

```sql
CREATE TABLE place_new (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  place_id   TEXT NOT NULL UNIQUE,
  name       TEXT NOT NULL,
  category   TEXT NOT NULL,
  address    TEXT,
  lat        REAL,
  lng        REAL,
  source_api TEXT
);
```

각 컬럼의 역할은 다음과 같다.

| 컬럼 | 역할 |
|---|---|
| `id` | DB 내부에서 사용하는 숫자 PK |
| `place_id` | 수집기 및 외부 API와 연결되는 기존 식별자 |

기존 데이터를 다시 수집할 필요는 없다. 기존 테이블의 데이터를 새 테이블로 복사하면 `id`가 자동 생성된다.

```sql
INSERT INTO place_new (
  place_id,
  name,
  category,
  address,
  lat,
  lng,
  source_api
)
SELECT
  place_id,
  name,
  category,
  address,
  lat,
  lng,
  source_api
FROM place;
```

예시 결과:

```text
id | place_id                       | name
---+--------------------------------+------
1  | daejeon_food_abc123            | 성심당
2  | 126508                         | 한밭수목원
```

숫자 PK로 실제 외래키 구조를 전환하려면 `place_tag`를 비롯하여 `place_id`를 참조하는 모든 테이블과 애플리케이션 코드를 함께 마이그레이션해야 한다.

## 8. 최종 권장사항

현재 태그 기능을 추가하는 목적이라면 다음 방식을 권장한다.

1. 기존 `place.place_id`를 PK로 유지한다.
2. `tag` 테이블에 숫자 `tag_id` PK를 사용한다.
3. `place_tag`에서 기존 `place_id`와 `tag_id`를 외래키로 연결한다.
4. 화면 표시용 순번은 `ROW_NUMBER()`로 생성한다.
5. 숫자형 장소 PK가 반드시 필요해질 때만 별도의 `id` 컬럼 추가와 전체 마이그레이션을 검토한다.

핵심은 다음과 같다.

> `place_id`는 줄 번호가 아니라 장소의 신분증 번호다. 보기 좋게 만들기 위해 재정렬하면 수집기, 외부 API, 태그 및 추천 데이터와의 연결이 깨질 수 있다.
