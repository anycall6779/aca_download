# 📦 아카라이브 게시글 다운로더 (Arca Live Downloader)

아카라이브 게시글을 이미지와 함께 ZIP 압축 파일로 저장해주는 GUI 도구입니다.  
본문 내용을 HTML로 보존하며, **ArcaRefresher 방식의 원본 화질 이미지 다운로드**와 **실시간 진행 상황 표시**를 지원합니다.

![arca_icon.png](arca_icon.png)

---

## ✨ 주요 기능

### 🖼️ 이미지 다운로드

| 모드 | 설명 |
|---|---|
| **원본 화질** (기본) | ArcaRefresher `ImageInfo.jsx` 방식 — `ac-o.namu.la` + `type=orig` 파라미터로 서버 원본 이미지 수신 |
| **미리보기 화질** | 체크 해제 시 CDN 리사이즈 이미지 (빠르지만 화질 낮음) |

> **원본 URL 결정 우선순위**: `data-originalurl` → `data-src` → `src`  
> **JPG 속도 최적화**: 너비 ≤ 1280 px 인 JPG/JPEG는 미리보기 URL 사용 (ArcaRefresher 동일 동작)

### 📊 실시간 진행 상황

```
전체   ████████░░░░  8 / 33 이미지      ⏱ 전체 4분 12초 남음
이미지  ██████████    1,024 / 6,053 KB  ⚡ 2.4 MB/s  ⏱ 1초 남음
```

- **전체 프로그레스바**: 처리된 이미지 수 / 전체 이미지 수
- **이미지 프로그레스바**: 현재 이미지의 다운로드 바이트 진행 (청크 단위 실시간)
- **다운로드 속도**: B/s → KB/s → MB/s 자동 단위 전환
- **현재 이미지 ETA**: 현재 이미지 완료까지 남은 시간
- **전체 ETA**: 완료된 이미지의 평균 크기·속도를 기반으로 전체 완료 예상 시간 계산

### ⏸ 다운로드 제어

| 버튼 | 동작 |
|---|---|
| **⬇ 다운로드 시작** | 입력된 URL 목록을 순차 처리 |
| **⏸ 일시정지** | 현재 청크 수신 후 대기, **▶ 재개** 클릭 시 재시작 |
| **⏹ 중지** | 확인 후 즉시 중단, 현재 이미지는 차단 |

세 버튼이 한 행에 배치되어 공간을 최소화합니다.

### 🔐 로그인 & 인증

- **Edge 브라우저 자동 로그인**: 내장 WebDriver로 아카라이브 로그인 후 쿠키 자동 수집
- **HTTP 451 우회**: 성인 인증·Cloudflare 봇 감지 통과
- **쿠키 직접 입력**: 수동으로 쿠키 문자열 붙여넣기도 지원

### 📁 저장 형식

다운로드 완료 시 `arca-{제목}.zip` 파일 생성:

```
arca-게시글제목.zip
├── post.html      ← 본문 + 이미지 레이아웃 보존 HTML
├── meta.txt       ← 제목, 작성자, 작성일, 원문 링크
└── images/
    ├── img_001.png
    ├── img_002.jpg
    └── ...
```

### 🔁 안정적인 재시도

- **최대 10회 자동 재시도** (ArcaRefresher `fetchWithRetry` 동일 방식)
- **재시도 간격**: 1초 고정 (429 Too Many Requests 포함)
- **순차 다운로드**: 이미지 간 병렬 요청 없이 순서대로 처리하여 429 완전 방지

---

## 🚀 사용 방법

### 1. 실행 파일 (Windows)

`dist/arca_downloader.exe` 파일을 실행하세요.

- **로그인 기능 사용 시** PC에 [Microsoft Edge](https://www.microsoft.com/ko-kr/edge)가 설치되어 있어야 합니다.
- (선택) `msedgedriver.exe`를 실행 파일과 같은 폴더에 두면 더 안정적으로 자동 로그인이 진행됩니다.

### 2. 소스 코드 직접 실행

Python 3.11 이상 필요.

```bash
# 의존성 설치
pip install requests beautifulsoup4 lxml pillow selenium webdriver-manager

# 실행
python arca_gui.py
```

### 3. 기본 사용 순서

1. **URL 입력** — `＋ URL 추가` 버튼으로 게시글 주소 추가 (여러 개 가능)
2. **(선택) 로그인** — 451 에러 발생 시 `🔑 아카라이브 로그인` 클릭
3. **저장 위치** — 폴더 선택
4. **화질 선택** — `🖼️ 이미지 원본 다운로드` 체크 여부 결정
5. **다운로드 시작** — `⬇ 다운로드 시작` 클릭

---

## 🔐 HTTP 451 해결 방법

게시글 다운로드 중 HTTP 451 오류 발생 시:

1. **`🔑 아카라이브 로그인`** 버튼 클릭
2. Edge 브라우저에서 아카라이브 로그인
3. 프로그램이 자동으로 쿠키를 수집하고 브라우저를 닫음 (`✅ 로그인됨` 표시 확인)
4. **`⬇ 다운로드 시작`** 재클릭

---

## 🛠 EXE 빌드 방법

```bash
pip install pyinstaller

pyinstaller --onefile --windowed --icon="arca_icon.ico" --add-data="arca_icon.png;." --name="arca_downloader" arca_gui.py
```

---

## 📋 요구 사항

- **Python**: 3.11+
- **라이브러리**:

| 패키지 | 용도 |
|---|---|
| `requests` | 웹 페이지 및 이미지 HTTP 요청 |
| `beautifulsoup4`, `lxml` | HTML 파싱 |
| `pillow` | 아이콘 및 이미지 처리 (GUI용) |
| `selenium`, `webdriver-manager` | Edge 브라우저 자동화 및 쿠키 수집 |

---

## ⚙️ 설정값 (arca_gui.py 상단)

| 상수 | 기본값 | 설명 |
|---|---|---|
| `MAX_WORKERS` | `3` | 미리보기 화질 병렬 다운로드 워커 수 |
| `FETCH_RETRY` | `10` | 이미지당 최대 재시도 횟수 |
| `FETCH_WAIT` | `1.0` | 재시도 간격 (초) |

---

## ⚠️ 주의 사항

- 이 도구는 **개인 소장 및 아카이브 목적**으로만 사용하시기 바랍니다.
- 저작권이 있는 콘텐츠의 무단 배포에 대한 책임은 사용자에게 있습니다.
- 아카라이브 서버에 과도한 부하를 주지 않도록 적절한 간격을 두고 사용하세요.

---

## 📄 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다.

원본 게시물 https://arca.live/b/3d3d/148920327 에서 영감을 받았습니다.
- Thx project this code https://github.com/lekakid/ArcaRefresher/
