# 📦 아카라이브 게시글 다운로더 (Arca Live Downloader)

아카라이브 게시글을 이미지와 함께 ZIP 압축 파일로 저장해주는 GUI 도구입니다. 본문 내용을 HTML로 보존하며, 다수의 이미지를 병렬로 빠르게 다운로드합니다.

![arca_icon.png](arca_icon.png)

## ✨ 주요 기능

- **다중 URL 지원**: 여러 개의 게시글 주소를 한 번에 입력하여 순차적으로 다운로드할 수 있습니다.
- **병렬 이미지 다운로드**: 멀티스레딩(ThreadPoolExecutor)을 사용하여 수십 장의 이미지도 순식간에 처리합니다.
- **본문 보존**: `post.html` 파일을 생성하여 원문의 레이아웃과 이미지를 오프라인에서도 확인할 수 있게 합니다.
- **메타데이터 저장**: `meta.txt`를 통해 제목, 작성자, 작성일, 원문 링크를 별도로 기록합니다.
- **스마트 프록시**: `namu.la` 등의 이미지 링크가 차단된 경우 자동으로 `weserv.nl` 프록시를 시도합니다.
- **모던 GUI**: 깔끔한 다크 테마 디자인과 실시간 로그 확인 기능을 제공합니다.

## 🚀 사용 방법

### 1. 실행 파일 사용 (Windows)
`dist/arca_downloader.exe` 파일을 실행하세요.

### 2. 소스 코드 실행
Python 3.11 이상의 환경이 필요합니다.

```bash
# 의존성 설치
pip install requests bs4 lxml pillow

# 실행
python arca_gui.py
```

## 🛠 빌드 방법 (EXE 생성)

PyInstaller를 사용하여 단일 실행 파일을 만들 수 있습니다.

```bash
pip install pyinstaller

pyinstaller --onefile --windowed --icon="arca_icon.ico" --add-data="arca_icon.png;." --name="arca_downloader" arca_gui.py
```

## 📋 요구 사항

- **Python**: 3.11+
- **Libraries**:
  - `requests`: 웹 페이지 및 이미지 요청
  - `beautifulsoup4`: HTML 파싱
  - `lxml`: 빠른 파싱 엔진
  - `pillow`: 아이콘 및 이미지 처리 (GUI용)

## ⚠️ 주의 사항

- 이 도구는 개인 소장 및 아카이브 목적으로만 사용하시기 바랍니다.
- 저작권이 있는 콘텐츠의 무단 배포에 대한 책임은 사용자에게 있습니다.
- 아카라이브 서버에 과도한 부하를 주지 않도록 적절한 간격을 두고 사용해 주세요.

## 📄 라이선스

이 프로젝트는 MIT 라이선스 하에 배포됩니다.
