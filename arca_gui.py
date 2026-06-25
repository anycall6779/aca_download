"""
arca_gui.py — 아카라이브 게시글 ZIP 저장기 (GUI, 다중 URL)
"""
import io, re, copy, zipfile, threading, base64, os, time, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse, parse_qs, urlencode, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests # type: ignore
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# ── 상수 ──────────────────────────────────────────────────────────────────────

BODY_SELECTORS = [
    '#article-content', '.article-content', '.content .fr-view',
    '.fr-view', '.article-body', '.content-body',
    'article .content', '.markdown-body', '.article .content-body',
]
STRIP_TAGS      = ['script','style','iframe','video','audio','noscript']
STRIP_SELECTORS = ['.btn','.buttons','.actions','.toolbar',
                   '.comment','.comments','.ad','[data-ad]']
IMAGE_EXTS      = {'png','jpg','jpeg','gif','webp','avif','bmp','svg'}
DEFAULT_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'),
    'Accept-Language': 'ko-KR,ko;q=0.9,en;q=0.8',
}
MAX_WORKERS   = 3   # 미리보기 화질: 이미지 병렬 수
FETCH_RETRY   = 10  # ArcaRefresher fetchWithRetry tryCount
FETCH_WAIT    = 1.0 # ArcaRefresher fetchWithRetry interval (1초)
CONFIG_PATH   = Path(__file__).parent / 'config.json'
ICON_PATH     = Path(__file__).parent / 'Icon.png'

# ── UI 상수 ───────────────────────────────────────────────────────────────────

BG      = '#13151f'
PANEL   = '#1c1f2e' # 카드보다 어두운 패널
CARD    = '#252839'
INPUT   = '#1a1d2a'
ACCENT  = '#5c7cfa'
TEXT    = '#ffffff'
MUTED   = '#a0a8c8'
SUCCESS = '#2cdb8f'
WARN    = '#fcbc30'
ERROR   = '#fa5252' # 기존 DEL_C와 동일
BORDER  = '#2e3350' # 카드 테두리
SEP     = '#23263a'
FONT_MAIN = '맑은 고딕'
FONT_CODE = '맑은 고딕'

# ── 다운로드 로직 ─────────────────────────────────────────────────────────────

def _make_session(base_url, cookie_str=''):
    s = requests.Session()
    s.headers.update({**DEFAULT_HEADERS, 'Referer': base_url})
    # 쿠키 문자열 파싱: "key=val; key2=val2" 형태
    if cookie_str.strip():
        for pair in cookie_str.split(';'):
            pair = pair.strip()
            if '=' in pair:
                k, v = pair.split('=', 1)
                s.cookies.set(k.strip(), v.strip(), domain='arca.live')
    retry = Retry(total=5, backoff_factor=1,
                  status_forcelist={429,500,502,503,504},
                  allowed_methods={'GET'}, raise_on_status=False)
    adp = HTTPAdapter(max_retries=retry,
                      pool_connections=MAX_WORKERS,
                      pool_maxsize=MAX_WORKERS*2)
    s.mount('http://', adp); s.mount('https://', adp)
    return s

def sanitize_filename(name: str, max_len: int = 80) -> str:
    """파일 이름으로 사용할 수 없는 문자를 공백으로 바꾸고 길이를 제한합니다."""
    # 파일 시스템에서 허용되지 않는 문자 제거: \ / : * ? " < > |
    # 이모지를 포함한 다른 유니코드 문자는 유지합니다.
    name = re.sub(r'[\\/:*?"<>|]', ' ', name)
    name = name.strip()
    return name[:max_len] or 'post'

def get_image_ext(src):
    try:
        ext = urlparse(src).path.rsplit('.',1)[-1].lower().split('?')[0].split('#')[0]
        if ext in IMAGE_EXTS: return ext
    except: pass
    return 'png'

def escape_html(s):
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def _get_article_info(url, cookie_str, log_func, on_error_func):
    """주어진 URL에서 게시글 제목과 BeautifulSoup 객체를 추출합니다. 세션도 함께 반환합니다."""
    try:
        session = _make_session(url, cookie_str=cookie_str)
        resp = session.get(url, timeout=10)
        if resp.status_code == 451:
            on_error_func(f'HTTP 451: 법적 사유로 차단된 페이지입니다.\n'
                          f'유효한 아카라이브 로그인 쿠키를 입력해야 접근할 수 있습니다. (URL: {url})')
            return None, None, None # Indicate failure
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'lxml')

        # <div class="title-row"> <div class="title"> 에서 제목 추출
        title_div = soup.select_one('div.title-row > div.title')
        if title_div:
            for span_tag in title_div.find_all('span'):
                span_tag.decompose()
            T = title_div.get_text(strip=True)
        else:
            og = soup.find('meta', property='og:title')
            T  = (og.get('content','') if og else '') or (soup.title.string if soup.title else '') or 'post'
            if ' - 아카라이브' in T:
                T = T.split(' - 아카라이브')[0].rsplit(' - ', 1)[0]
            T = T.strip()
        return (T if T else 'unknown_article'), soup, session
    except Exception as e:
        log_func(f'[WARN] URL {url} 에서 정보 추출 실패: {e}')
        on_error_func(f'게시글 정보 추출 중 오류 발생 (URL: {url}): {e}')
        return None, None, None # Indicate failure

# ── ArcaRefresher ImageDownloader 이식 ────────────────────────────────────────────
#
# ImageInfo.jsx 핵심 로직 이식:
#  1. URL = data-originalurl → data-src → src 순서로 탐색
#  2. 원본 URL 구성: ac-o.namu.la 호스트 + type=orig 파라미터
#  3. JPG 속도 최적화: 너비 ≤1280px JPG/JPEG는 미리보기 URL 사용
#
# DownloadDialog.jsx 핵심 로직 이식:
#  • 스트리밍 순차 다운로드 (highWaterMark: 0, 이미지 1개씩)
#  • fetchWithRetry — 실패 시 1초 간격으로 최대 10회 재시도

def get_arcaimg_url(img_tag, base_url, download_original=True):
    """ArcaRefresher ImageInfo.jsx 이식.
    data-originalurl → data-src → src 순서로 URL 결정 후
    필요 시 ac-o.namu.la + type=orig 적용.
    JPG 너비 ≤1280px 수도 최적화 제외."""
    raw = img_tag.get('data-originalurl') or img_tag.get('data-src') or img_tag.get('src', '')
    if not raw:
        return ''
    try:
        src = urljoin(base_url, raw)
        parsed = urlparse(src)
        ext = parsed.path.rsplit('.', 1)[-1].lower().split('?')[0] if '.' in parsed.path else ''

        if not download_original:
            return src

        # JPG 속도 최적화: 너비 ≤1280px인 JPG/JPEG는 미리보기 URL 그대로 사용
        if ext in ('jpg', 'jpeg'):
            try:
                width = int(img_tag.get('width') or 0)
            except (ValueError, TypeError):
                width = 0
            if width <= 1280 and width > 0:  # 너비 정보가 있는 소형 JPG
                return src  # 미리보기 사용 (속도 최적화)

        # ac-o.namu.la + type=orig 적용
        if any(d in parsed.netloc for d in ('namu.la', 'arca.live')):
            netloc = 'ac-o.namu.la'
            qs = parse_qs(parsed.query)
            qs['type'] = ['orig']
            return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params,
                               urlencode(qs, doseq=True), parsed.fragment))
        return src
    except Exception:
        return raw


def fetch_image(session, src, log, chunk_cb=None, stop_event=None, pause_event=None):
    """ArcaRefresher fetchWithRetry 이식.
    스트리밍 순차 다운로드, 실패 시 1초 간격으로 최대 10회 재시도.
    chunk_cb(downloaded, total)  — 청크마다 호출되는 진행 콜백
    stop_event / pause_event     — threading.Event 중지/일시정지 신호"""
    for attempt in range(1, FETCH_RETRY + 1):
        # 중지 신호 확인
        if stop_event and stop_event.is_set():
            return None
        try:
            with session.get(src, timeout=60, stream=True) as r:
                if r.status_code == 429:
                    log(f'    [WARN] 429 — {FETCH_WAIT}s 대기 재시도 ({attempt}/{FETCH_RETRY})')
                    time.sleep(FETCH_WAIT)
                    continue
                r.raise_for_status()

                # Content-Length 확인 (없으면 -1)
                total_bytes = int(r.headers.get('Content-Length', -1))
                buf = bytearray()
                for chunk in r.iter_content(8192):
                    # 일시정지 대기
                    if pause_event:
                        while pause_event.is_set() and not (stop_event and stop_event.is_set()):
                            time.sleep(0.1)
                    # 중지
                    if stop_event and stop_event.is_set():
                        return None
                    buf.extend(chunk)
                    if chunk_cb:
                        chunk_cb(len(buf), total_bytes)
                return bytes(buf)
        except Exception as e:
            if stop_event and stop_event.is_set():
                return None
            if attempt < FETCH_RETRY:
                log(f'    [WARN] 실패: {e} — {FETCH_WAIT}s 후 재시도 ({attempt}/{FETCH_RETRY})')
                time.sleep(FETCH_WAIT)
            else:
                log(f'    [WARN] 최종 실패 ({FETCH_RETRY}회): {e}')
    return None


def download_article(url, output_dir, log, set_progress, on_done, on_error,
                     session, pre_fetched_title, pre_fetched_soup,
                     cookie_str='', download_original=True,
                     add_id_to_filename=False, set_img_progress=None, set_total_eta=None,
                     stop_event=None, pause_event=None):
    try:
        log(f'[*] 요청: {url}')
        if cookie_str.strip():
            log('    (쿠키 인증 사용 중)')

        T = pre_fetched_title
        soup = pre_fetched_soup
        if not T or not soup:
            on_error("미리 가져온 제목 또는 soup 객체가 없습니다."); return

        ae = (soup.find(rel='author') or
              soup.select_one('.article-header .user,.user-info .nick,.writer,.author'))
        am = soup.find('meta', attrs={'name':'author'})
        A  = (ae.get_text(strip=True) if ae else '') or (am.get('content','') if am else '')

        te = soup.find('time', datetime=True)
        D  = te.get('datetime','').strip() if te else ''
        if not D:
            te2 = soup.find('time')
            D = te2.get_text(strip=True) if te2 else ''
        if not D:
            de = soup.select_one('.date,.time,.article-info time')
            D  = de.get_text(strip=True) if de else ''

        U = url.split('#')[0]
        log(f'    제목  : {T}'); log(f'    작성자: {A or "Unknown"}'); log(f'    작성일: {D or "Unknown"}')

        body = None
        for sel in BODY_SELECTORS:
            el = soup.select_one(sel)
            if el: body = el; log(f'[*] 본문: {sel}'); break
        if body is None:
            on_error('본문을 찾지 못했어요.'); return

        content = copy.deepcopy(body)
        for tag in content.find_all(STRIP_TAGS): tag.decompose()
        for sel in STRIP_SELECTORS:
            for el in content.select(sel): el.decompose()
        for img in content.find_all('img'):
            resolved = get_arcaimg_url(img, url, download_original=download_original)
            if resolved:
                img['src'] = resolved
            elif not img.get('src') and img.get('data-src'):
                img['src'] = img['data-src']

        imgs  = [i for i in content.find_all('img') if i.get('src')]
        total = len(imgs)
        if download_original:
            log(f'[*] 이미지 {total}개 순차 다운로드 (ArcaRefresher 방식, 최대 {FETCH_RETRY}회 재시도)...')
        else:
            log(f'[*] 이미지 {total}개 다운로드 (워커 {min(MAX_WORKERS,max(total,1))}개)...')
        set_progress(0, total)

        tasks = []
        for idx, img in enumerate(imgs, 1):
            src  = urljoin(url, img['src'])
            name = f'img_{str(idx).zfill(3)}.{get_image_ext(src)}'
            tasks.append((idx, img, src, name))

        results = {}
        done_n  = 0
        lock    = threading.Lock()

        zip_buf = io.BytesIO(); downloaded = []

        if download_original:
            # 원본 화질: 순차 다운로드 (429 방지)
            global_start     = time.time()        # 전체 다운 시작
            completed_sizes  = []                  # 완료된 이미지 바이트 크기
            
            # 부드러운 속도 표시를 위한 변수
            self._last_speed_update_time = 0
            self._last_speed_update_bytes = 0

            for task in tasks:
                if stop_event and stop_event.is_set():
                    on_error('사용자가 다운로드를 중지했습니다.')
                    return
                idx, img, src, name = task
                log(f'  [{idx:03d}/{total}] {src}')

                img_start_time = time.time()
                speed_bps      = [0.0]

                def _chunk_cb(dl_bytes, total_b,
                              _img_start=img_start_time, _speed=speed_bps,
                              _idx=idx):
                    elapsed_img = time.time() - _img_start
                    
                    # 0.5초마다 속도 갱신하여 부드럽게 표시
                    now = time.time()
                    if now - self._last_speed_update_time > 0.5:
                        if self._last_speed_update_time > 0:
                            time_delta = now - self._last_speed_update_time
                            byte_delta = dl_bytes - self._last_speed_update_bytes
                            _speed[0] = byte_delta / time_delta if time_delta > 0 else 0
                        
                        self._last_speed_update_time = now
                        self._last_speed_update_bytes = dl_bytes
                        set_img_progress(dl_bytes, total_b, _speed[0])
                    # 전체 ETA — 완료 이미지 평균 크기 기반
                    if set_total_eta and total_b > 0:
                        elapsed_total = time.time() - global_start
                        n_done = len(completed_sizes)
                        fraction = dl_bytes / total_b           # 현재 이미지 진행 비율
                        total_dl = sum(completed_sizes) + dl_bytes
                        imgs_done_equiv = n_done + fraction     # 실효 완료량 (1.0 = 1개)
                        if elapsed_total > 0 and imgs_done_equiv > 0:
                            avg_img_size  = total_dl / imgs_done_equiv
                            overall_speed = total_dl / elapsed_total
                            remaining     = (total - _idx + 1 - fraction) * avg_img_size
                            set_total_eta(remaining / overall_speed if overall_speed > 0 else 0)

                data = fetch_image(session, src, log,
                                   chunk_cb=_chunk_cb,
                                   stop_event=stop_event,
                                   pause_event=pause_event)
                if stop_event and stop_event.is_set():
                    on_error('사용자가 다운로드를 중지했습니다.')
                    return
                if data:
                    completed_sizes.append(len(data))
                else:
                    log('       → 건너뜀')
                results[idx] = (name, data)
                done_n += 1
                set_progress(done_n, total)
                if set_img_progress:
                    set_img_progress(0, 0, 0)  # 다음 이미지 전 리셋
                
                # 다음 이미지를 위해 속도 업데이트 변수 초기화
                self._last_speed_update_time = 0
                self._last_speed_update_bytes = 0


        else:
            # 미리보기 화질: 병렬 다운로드
            last_request_time = 0
            time_lock = threading.Lock()

            def _dl(task):
                nonlocal done_n, last_request_time
                idx, img, src, name = task
                with time_lock:
                    now = time.time()
                    elapsed = now - last_request_time
                    wait_time = 0.5 - elapsed
                    if wait_time > 0:
                        time.sleep(wait_time)
                    last_request_time = time.time()

                log(f'  [{idx:03d}/{total}] {src}')
                data = fetch_image(session, src, log)
                if not data:
                    log('       → 건너뜀')
                with lock:
                    done_n += 1; set_progress(done_n, total)
                return idx, name, data

            with ThreadPoolExecutor(max_workers=min(MAX_WORKERS,max(total,1))) as pool:
                for fut in as_completed({pool.submit(_dl,t):t[0] for t in tasks}):
                    idx, name, data = fut.result(); results[idx] = (name, data)

        with zipfile.ZipFile(zip_buf,'w',zipfile.ZIP_DEFLATED) as zf:
            for idx, img in enumerate(imgs,1):
                name, data = results.get(idx,(f'img_{str(idx).zfill(3)}.png',None))
                if data:
                    zf.writestr(f'images/{name}', data)
                    img['src'] = f'images/{name}'
                    for a in ('srcset','data-src','loading'):
                        img.attrs.pop(a, None)
                    downloaded.append(name)

            eT,eD,eA,eU = escape_html(T),escape_html(D or 'Unknown'),escape_html(A or 'Unknown'),escape_html(U)
            hdr_html = (f'<header style="font:14px/1.5 system-ui,sans-serif;border-bottom:1px solid #ddd;padding:12px 0;margin-bottom:16px;">'
                        f'<div><strong>제목</strong>: {eT}</div><div><strong>작성일</strong>: {eD}</div>'
                        f'<div><strong>작성자</strong>: {eA}</div>'
                        f'<div><strong>원문</strong>: <a href="{eU}">{eU}</a></div></header>')
            html = (f'<!doctype html><html lang="ko"><head><meta charset="utf-8">'
                    f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>{eT}</title>'
                    f'<style>body{{max-width:960px;margin:0 auto;padding:24px;'
                    f'font:16px/1.7 system-ui,sans-serif;color:#111;background:#fff}}'
                    f'img{{max-width:100%;height:auto}}'
                    f'pre,code{{white-space:pre-wrap;word-break:break-word}}'
                    f'table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:6px}}'
                    f'</style></head><body>{hdr_html}<main>{content.decode_contents()}</main></body></html>')
            zf.writestr('post.html', html.encode('utf-8'))
            img_line = f'Images: {len(downloaded)} files under /images' if downloaded else 'Images: (none downloaded)'
            zf.writestr('meta.txt', '\n'.join([f'Title: {T}',f'Author: {A or "Unknown"}',
                                               f'Date: {D or "Unknown"}',f'Source: {U}',img_line]).encode('utf-8'))

        filename = sanitize_filename(T)
        if add_id_to_filename:
            article_id = url.split('/')[-1].split('?')[0].split('#')[0]
            if article_id.isdigit():
                filename += f' [{article_id}]'
        out_path = Path(output_dir) / f'{filename}.zip'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(zip_buf.getvalue())
        on_done(str(out_path.resolve()), len(downloaded), total)

    except Exception as e:
        on_error(str(e))


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('아카라이브 다운로더')
        self.geometry('640x860')
        self.minsize(640, 860)
        self.configure(bg=BG)
        self.resizable(True, True)

        # Windows 고해상도(HiDPI) 지원
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass # Windows 아닌 경우 등

        # 아카라이브 아이콘 설정
        if ICON_PATH.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(ICON_PATH).resize((32,32), Image.LANCZOS)
                self._icon = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._icon)
            except Exception:
                pass

        self._downloading   = False
        self._stop_event    = threading.Event()   # 중지
        self._pause_event   = threading.Event()   # 수동: set=일시정지, clear=계속다운

        self._build_styles()
        self._build_ui()
        self._load_config() # 설정 로드 추가
        self.protocol("WM_DELETE_WINDOW", self._on_closing)

    # ── 스타일 ───────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('TFrame',       background=BG)
        s.configure('Panel.TFrame', background=PANEL) # type: ignore
        s.configure('TLabel',       background=BG,    foreground=TEXT,  font=(FONT_MAIN,10))
        s.configure('H1.TLabel',    background=BG,    foreground=TEXT,  font=(FONT_MAIN,17,'bold'))
        s.configure('Muted.TLabel', background=BG,    foreground=MUTED, font=(FONT_MAIN,9))
        s.configure('TProgressbar',     troughcolor=PANEL, background=ACCENT,  thickness=4,  borderwidth=0)
        s.configure('Sub.TProgressbar', troughcolor=PANEL, background=SUCCESS, thickness=6,  borderwidth=0)
        # 레이아웃을 TProgressbar에서 복사 (없으면 'Horizontal.Sub.TProgressbar not found' 오류)
        s.layout('Sub.TProgressbar', s.layout('Horizontal.TProgressbar'))

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self, bg=BG)
        outer.pack(fill='both', expand=True, padx=28, pady=24)

        # ── 헤더 ─────────────────────────────────────────────────────────
        hdr = tk.Frame(outer, bg=BG)
        hdr.pack(fill='x', pady=(0,16)) # 여백 감소

        # 아이콘 + 제목
        if ICON_PATH.exists():
            try:
                from PIL import Image, ImageTk
                ico = Image.open(ICON_PATH).resize((36,36), Image.LANCZOS)
                self._hdr_icon = ImageTk.PhotoImage(ico)
                tk.Label(hdr, image=self._hdr_icon, bg=BG).pack(side='left', padx=(0,10))
            except: pass

        title_col = tk.Frame(hdr, bg=BG)
        title_col.pack(side='left')
        tk.Label(title_col, text='아카라이브 다운로더',
                 bg=BG, fg=TEXT, font=(FONT_MAIN,17,'bold')).pack(anchor='w')
        tk.Label(title_col, text='게시글 URL을 입력하면 이미지 포함 ZIP으로 저장합니다',
                 bg=BG, fg=MUTED, font=(FONT_MAIN,9)).pack(anchor='w')

        # --- 로그인 위젯 컨테이너 (헤더 오른쪽에 배치) ---
        login_frame = tk.Frame(hdr, bg=BG)
        login_frame.pack(side='right', anchor='n', pady=0)

        # 상태 표시 라벨 (상단)
        self.login_status_var = tk.StringVar(value='미로그인')
        self.login_status_lbl = tk.Label(
            login_frame, textvariable=self.login_status_var,
            bg=BG, fg=MUTED,
            font=(FONT_MAIN, 9), anchor='e'
        )
        self.login_status_lbl.pack(anchor='e', pady=(0, 2))

        # 로그인/로그아웃 버튼 (하단)
        self.login_btn = tk.Button(
            login_frame, text='아카라이브 로그인',
            command=self._do_login,
            bg=ACCENT, fg=TEXT,
            activebackground=ACCENT, activeforeground=TEXT,
            font=(FONT_MAIN, 9, 'bold'),
            relief='flat', cursor='hand2', padx=12, pady=6, bd=0
        )
        self.login_btn.pack(fill='x')
        # ------------------------------------------------

        # ── URL 섹션 ──────────────────────────────────────────────────────
        self._section_label(outer, 'URL 목록')

        url_card = tk.Frame(outer, bg=CARD,
                            highlightbackground=BORDER, highlightthickness=1)
        url_card.pack(fill='x', pady=(4,12))

        url_top_row = tk.Frame(url_card, bg=CARD)
        url_top_row.pack(fill='x', padx=14, pady=(12,6))

        tk.Label(url_top_row, text='다운로드 할 게시글 URL을(다수의 URL은 , 로 구분) 입력하세요.',
                 bg=CARD, fg=MUTED, font=(FONT_MAIN,9)).pack(side='left')

        self._btn(url_top_row, '파일에서 불러오기', self._load_urls_from_file,
                  bg=PANEL, fg=MUTED, side='right', padx=10, font_size=8)

        self.url_text = tk.Text(url_card, height=4,
                                bg=INPUT, fg=TEXT, insertbackground=TEXT, # type: ignore
                                font=(FONT_MAIN, 10), relief='flat',
                                highlightbackground=BORDER, highlightthickness=1,
                                selectbackground=BORDER, wrap='word', padx=8, pady=6)
        self.url_text.pack(fill='x', padx=14, pady=(0,12))
        self.url_text.bind('<Return>', lambda e: self._start_download())

        self.cookie_var = tk.StringVar()  # 내부 쿠키 저장용 (UI에 직접 표시 안 함)

        # ── 저장 위치 ─────────────────────────────────────────────────────
        self._section_label(outer, '저장 위치')

        dir_card = tk.Frame(outer, bg=CARD,
                            highlightbackground=BORDER, highlightthickness=1)
        dir_card.pack(fill='x', pady=(4,12)) # 여백 감소

        dir_row = tk.Frame(dir_card, bg=CARD)
        dir_row.pack(fill='x', padx=14, pady=12)

        self.dir_var = tk.StringVar(value=str(Path.home()/'Downloads'))
        tk.Entry(dir_row, textvariable=self.dir_var,
                 bg=INPUT, fg=TEXT, insertbackground=TEXT,
                 font=(FONT_MAIN,10), relief='flat',
                 highlightbackground=BORDER, highlightthickness=1
                 ).pack(side='left', fill='x', expand=True, ipady=8, padx=(0,8))
        self._btn(dir_row, '폴더 선택', self._browse_dir,
                  bg=ACCENT, side='left')

        # ── 다운로드 설정 ─────────────────────────────────────────────────
        self._section_label(outer, '다운로드 설정')

        settings_card = tk.Frame(outer, bg=CARD,
                                 highlightbackground=BORDER, highlightthickness=1)
        settings_card.pack(fill='x', pady=(4,12)) # 여백 감소

        settings_row = tk.Frame(settings_card, bg=CARD)
        settings_row.pack(fill='x', padx=14, pady=12)

        self.orig_img_var = tk.BooleanVar(value=True)
        self.orig_img_cb = tk.Checkbutton(
            settings_row,
            text='이미지 원본 다운로드 (체크 해제 시 미리보기 화질로 다운로드)',
            variable=self.orig_img_var,
            bg=CARD,
            fg=TEXT,
            selectcolor=INPUT,
            activebackground=CARD,
            activeforeground=TEXT,
            font=(FONT_MAIN, 10),
            relief='flat',
            bd=0,
            cursor='hand2'
        )
        self.orig_img_cb.pack(side='left')

        settings_row2 = tk.Frame(settings_card, bg=CARD)
        settings_row2.pack(fill='x', padx=14, pady=(0, 12))

        self.add_id_var = tk.BooleanVar(value=False)
        self.add_id_cb = tk.Checkbutton(
            settings_row2,
            text='파일 이름에 게시글 ID 추가 (예: 제목 [12345].zip)',
            variable=self.add_id_var,
            bg=CARD,
            fg=TEXT,
            selectcolor=INPUT,
            activebackground=CARD,
            activeforeground=TEXT,
            font=(FONT_MAIN, 10),
            relief='flat', bd=0, cursor='hand2'
        )
        self.add_id_cb.pack(side='left')
        settings_row3 = tk.Frame(settings_card, bg=CARD)
        settings_row3.pack(fill='x', padx=14, pady=(0, 12))

        self.notify_var = tk.BooleanVar(value=True)
        self.notify_cb = tk.Checkbutton(
            settings_row3,
            text='다운로드 완료 시 알림',
            variable=self.notify_var,
            bg=CARD,
            fg=TEXT,
            selectcolor=INPUT,
            activebackground=CARD,
            activeforeground=TEXT,
            font=(FONT_MAIN, 10),
            relief='flat', bd=0, cursor='hand2'
        )
        self.notify_cb.pack(side='left')

        # ── 다운로드 제어 행 (시작 + 일시정지 + 중지) ──────────────────────
        ctrl_bar = tk.Frame(outer, bg=BG)
        ctrl_bar.pack(fill='x', pady=(0, 10))

        # 8:1:1 비율을 위해 grid 레이아웃 사용
        ctrl_bar.grid_columnconfigure(0, weight=8)
        ctrl_bar.grid_columnconfigure(1, weight=1)
        ctrl_bar.grid_columnconfigure(2, weight=1)

        self.dl_btn = tk.Button(
            ctrl_bar, text='다운로드 시작',
            command=self._start_download,
            bg=ACCENT, fg=TEXT,
            activebackground=ACCENT, activeforeground=TEXT,
            font=(FONT_MAIN, 9, 'bold'),
            relief='flat', cursor='hand2', pady=8, bd=0
        )
        self.dl_btn.grid(row=0, column=0, sticky='ew', padx=(0, 4))

        self.pause_btn = tk.Button(
            ctrl_bar, text='일시정지',
            command=self._toggle_pause,
            bg=WARN, fg=INPUT,
            disabledforeground=BORDER,
            activebackground=WARN, activeforeground=INPUT,
            font=(FONT_MAIN, 9, 'bold'),
            relief='flat', cursor='hand2', pady=8, bd=0, state='disabled'
        )
        self.pause_btn.grid(row=0, column=1, sticky='ew', padx=(4, 4))

        self.stop_btn = tk.Button(
            ctrl_bar, text='작업중지',
            command=self._stop_download,
            bg=ERROR, fg=TEXT,
            disabledforeground=BORDER,
            activebackground=ERROR, activeforeground=TEXT,
            font=(FONT_MAIN, 9, 'bold'),
            relief='flat', cursor='hand2', pady=8, bd=0, state='disabled'
        )
        self.stop_btn.grid(row=0, column=2, sticky='ew', padx=(4, 0))

        # ── 진행 파널 (2줄 콤팩트) ──────────────────────────────────
        prog_card = tk.Frame(outer, bg=CARD,
                             highlightbackground=BORDER, highlightthickness=1)
        prog_card.pack(fill='x', pady=(0, 8))

        # 전체 현황 행
        row_total = tk.Frame(prog_card, bg=CARD)
        row_total.pack(fill='x', padx=14, pady=(8, 3))
        tk.Label(row_total, text='전체', bg=CARD, fg=MUTED,
                 font=(FONT_MAIN, 8, 'bold'), width=4, anchor='w').pack(side='left')
        self.prog_var = tk.DoubleVar(value=0)
        ttk.Progressbar(row_total, variable=self.prog_var,
                        maximum=100, style='TProgressbar').pack(side='left', fill='x', expand=True, padx=(6, 8))
        self.total_eta_label = tk.Label(row_total, text='', bg=CARD, fg=MUTED,
                                        font=(FONT_MAIN, 8))
        self.total_eta_label.pack(side='right')
        self.prog_label = tk.Label(row_total, text='', bg=CARD, fg=MUTED,
                                   font=(FONT_MAIN, 8), width=14, anchor='e')
        self.prog_label.pack(side='right', padx=(0, 6))

        # 현재 이미지 행 (프로그레스 + KB 표시 + 속도 + ETA 한 행)
        row_img = tk.Frame(prog_card, bg=CARD)
        row_img.pack(fill='x', padx=14, pady=(0, 8))
        tk.Label(row_img, text='이미지', bg=CARD, fg=MUTED,
                 font=(FONT_MAIN, 8, 'bold'), width=4, anchor='w').pack(side='left')
        self.sub_prog_var = tk.DoubleVar(value=0)
        ttk.Progressbar(row_img, variable=self.sub_prog_var,
                        maximum=100, style='Sub.TProgressbar').pack(side='left', fill='x', expand=True, padx=(6, 8))
        self.eta_label = tk.Label(row_img, text='', bg=CARD, fg=MUTED,
                                  font=(FONT_MAIN, 8))
        self.eta_label.pack(side='right')
        self.speed_label = tk.Label(row_img, text='', bg=CARD, fg=MUTED,
                                    font=(FONT_MAIN, 8))
        self.speed_label.pack(side='right', padx=(0, 6))
        self.sub_prog_label = tk.Label(row_img, text='', bg=CARD, fg=MUTED,
                                       font=(FONT_MAIN, 8), width=16, anchor='e')
        self.sub_prog_label.pack(side='right', padx=(0, 4))



        # ── 로그 ─────────────────────────────────────────────────────────
        log_top = tk.Frame(outer, bg=BG)
        log_top.pack(fill='x')
        tk.Label(log_top, text='실행 로그', bg=BG, fg=MUTED,
                 font=(FONT_MAIN,9,'bold')).pack(side='left')
        self._btn(log_top, '지우기', self._clear_log,
                  bg=PANEL, fg=MUTED, side='right', padx=8, font_size=8)

        log_wrap = tk.Frame(outer, bg=INPUT,
                            highlightbackground=BORDER, highlightthickness=1)
        log_wrap.pack(fill='both', expand=True, pady=(4,0))
        self.log_text = tk.Text(log_wrap, bg=INPUT, fg=TEXT,
                                font=(FONT_CODE,9), relief='flat',
                                wrap='word', state='disabled',
                                selectbackground=BORDER,
                                insertbackground=TEXT, padx=12, pady=10)
        self.log_text.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(log_wrap, command=self.log_text.yview)
        sb.pack(side='right', fill='y')
        self.log_text['yscrollcommand'] = sb.set

        self.log_text.tag_configure('info',    foreground=TEXT)
        self.log_text.tag_configure('warn',    foreground=WARN)
        self.log_text.tag_configure('success', foreground=SUCCESS)
        self.log_text.tag_configure('error',   foreground=ERROR)
        self.log_text.tag_configure('sub',     foreground=MUTED)

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    def _load_config(self):
        """config.json 파일에서 저장된 설정을 로드합니다."""
        if not CONFIG_PATH.exists():
            return
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                config = json.load(f)

            cookie_str = config.get('cookie_str', '')
            if cookie_str:
                self.cookie_var.set(cookie_str)
                self._set_login_status(f'로그인됨 (자동)', SUCCESS)
                self._log('[*] 저장된 로그인 정보로 자동 로그인했습니다.')

            saved_dir = config.get('download_dir', '')
            if saved_dir and os.path.isdir(saved_dir):
                self.dir_var.set(saved_dir)
                self._log(f'[*] 저장된 경로를 불러왔습니다: {saved_dir}')
            
            self.orig_img_var.set(config.get('download_original', True))
            self.add_id_var.set(config.get('add_id_to_filename', False))
            self.notify_var.set(config.get('notify_on_complete', True))

        except Exception as e:
            self._log(f'[✗] 설정 파일 로드 실패: {e}')

    def _save_config(self):
        """현재 설정을 config.json 파일에 저장합니다."""
        try:
            config = {
                'cookie_str': self.cookie_var.get(),
                'download_dir': self.dir_var.get(),
                'download_original': self.orig_img_var.get(),
                'add_id_to_filename': self.add_id_var.get(),
                'notify_on_complete': self.notify_var.get(),
            }
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
        except Exception as e:
            self._log(f'[✗] 설정 파일 저장 실패: {e}')

    def _section_label(self, parent, text):
        f = tk.Frame(parent, bg=BG)
        f.pack(fill='x', pady=(0,2))
        tk.Label(f, text=text, bg=BG, fg=MUTED,
                 font=(FONT_MAIN,9,'bold')).pack(side='left') # bold 추가
        tk.Frame(f, bg=SEP, height=1).pack(side='left', fill='x', expand=True, padx=(8,0), pady=6)

    def _btn(self, parent, text, cmd, bg=None, fg=None,
             side='left', padx=10, font_size=9, active_fg=None):
        bg = bg or ACCENT
        fg = fg or TEXT
        tk.Button(parent, text=text, command=cmd, bg=bg, fg=fg,
                  activebackground=bg, activeforeground=(active_fg or fg),
                  font=(FONT_MAIN, font_size, 'bold'),
                  relief='flat', cursor='hand2', padx=padx, pady=5, bd=0
                  ).pack(side=side)

    def _clip(self):
        try: return self.clipboard_get().strip()
        except: return ''

    def _set_login_status(self, text, color):
        """로그인 상태 라벨과 버튼의 상태를 업데이트합니다."""
        # self.after를 사용하지 않아도 메인 스레드에서 안전하게 호출됩니다.
        self.login_status_var.set(text)
        self.login_status_lbl.configure(fg=color)

        # 로그인 상태에 따라 버튼 텍스트, 색상, 기능 변경
        if '로그인됨' in text:
            self.login_btn.config(
                text='로그아웃',
                command=self._clear_login,
                bg=INPUT, fg=MUTED,
                activebackground=BORDER
            )
        else:  # 미로그인 또는 오류 상태
            self.login_btn.config(
                text='아카라이브 로그인',
                command=self._do_login,
                bg=ACCENT, fg=TEXT,
                activebackground=ACCENT, activeforeground=TEXT
            )

    def _clear_login(self):
        self.cookie_var.set('')
        self._set_login_status('미로그인', MUTED)
        self._save_config() # 쿠키 삭제 후 설정 저장
        self._log('[*] 로그인 정보 삭제됨')

    def _do_login(self):
        """Selenium 브라우저를 열어 로그인 후 쿠키 자동 수집."""
        if self.cookie_var.get():
            if not messagebox.askyesno('재로그인', '이미 로그인되어 있습니다.\n다시 로그인하시겠습니까?'):
                return

        self.login_btn.configure(state='disabled', text='브라우저 열는 중...')
        self._set_login_status('로그인 대기 중', WARN)
        self._log('[*] 아카라이브 로그인 브라우저 열기 중...')

        def _run():
            try:
                driver = self._open_browser('https://arca.live/u/login')
            except RuntimeError as e:
                _e = str(e)
                self.after(0, lambda msg=_e: (
                    self._set_login_status('브라우저 오류', ERROR),
                    self._log(f'[✗] {msg}'),
                    self.login_btn.configure(state='normal', text='아카라이브 로그인')
                ))
                return

            self.after(0, lambda: (
                self._log('[*] 브라우저에서 로그인하시면 자동으로 쿠키를 가져옵니다.'),
            ))

            import time
            LOGIN_URL = 'https://arca.live/u/login'
            try:
                for _ in range(180):   # 최대 6분 대기
                    time.sleep(2)
                    try:
                        current = driver.current_url
                    except Exception:
                        break   # 브라우저 종료됨
                    # 로그인 성공 = /u/login 에서 다른 곳으로 리다이렉트
                    if 'login' not in current:
                        time.sleep(1)  # 쿠키 정착 대기
                        raw = driver.get_cookies()
                        cookie_str = '; '.join(f"{c['name']}={c['value']}" for c in raw)
                        n = len(raw)
                        def _ok(cs=cookie_str, n=n):
                            self.cookie_var.set(cs)
                            self._save_config() # 쿠키 저장 후 설정 저장
                            self._set_login_status(f'로그인됨 ({n}개)', SUCCESS)
                            self._log(f'[✓] 쿠키 {n}개 수집 완료! 이제 다운로드하세요.')
                            self.login_btn.configure(state='normal', text='다시 로그인')
                        self.after(0, _ok)
                        time.sleep(2)
                        break
                else:
                    self.after(0, lambda: ( # type: ignore
                        self._set_login_status('시간 초과', ERROR),
                        self._log('[!] 6분 내 로그인 감지 실패. 다시 시도해주세요.'),
                        self.login_btn.configure(state='normal', text='아카라이브 로그인')
                    ))
            finally:
                try: driver.quit()
                except: pass

        threading.Thread(target=_run, daemon=True).start()

    def _open_browser(self, url: str):
        """Edge를 Cloudflare 우회 모드로 열고 driver 반환."""
        try:
            from selenium import webdriver
            from selenium.webdriver.edge.options import Options as EO
            from selenium.webdriver.edge.service import Service as ES
        except ImportError:
            raise RuntimeError(
                'selenium 패키지가 필요합니다.\n'
                '터미널: pip install selenium'
            )

        opts = EO()
        # ── Cloudflare 봇 감지 우회 ────────────────────────────────────
        opts.add_argument('--disable-blink-features=AutomationControlled')
        opts.add_experimental_option('excludeSwitches', ['enable-automation'])
        opts.add_experimental_option('useAutomationExtension', False)

        # ── 기존 Edge 프로필 재사용 (Cloudflare 신뢰도 ↑) ────────────────
        edge_profile = Path.home() / 'AppData' / 'Local' / 'Microsoft' / 'Edge' / 'User Data'
        if edge_profile.exists():
            opts.add_argument(f'--user-data-dir={edge_profile}')
            opts.add_argument('--profile-directory=Default')

        opts.add_argument('--no-sandbox')
        opts.add_argument('--disable-dev-shm-usage')
        opts.add_argument('--start-maximized')

        # ── 드라이버 탐색 순서 ─────────────────────────────────────────
        driver = None
        last_err = ''

        # 스크립트 폴더 포함 탐색 경로 목록
        script_dir = str(Path(__file__).parent / 'msedgedriver.exe')
        search_paths = [
            None,          # PATH 자동 탐색
            script_dir,    # 스크립트와 같은 폴더 ← 우선 탐색
            str(Path.home() / 'Downloads' / 'msedgedriver.exe'),
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedgedriver.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedgedriver.exe',
        ]

        def _try_start(options):
            """주어진 options으로 드라이버 목록을 순서대로 시도."""
            for drv_path in search_paths:
                try:
                    svc = ES(drv_path) if drv_path else ES()
                    return webdriver.Edge(service=svc, options=options)
                except Exception as e:
                    nonlocal last_err
                    last_err = str(e)
            return None

        # 1차 시도: 기존 Edge 프로필 포함
        driver = _try_start(opts)

        # 2차 시도: 프로필 충돌 가능성 → 프로필 없이 재시도
        if driver is None:
            opts_no_profile = EO()
            opts_no_profile.add_argument('--disable-blink-features=AutomationControlled')
            opts_no_profile.add_experimental_option('excludeSwitches', ['enable-automation'])
            opts_no_profile.add_experimental_option('useAutomationExtension', False)
            opts_no_profile.add_argument('--no-sandbox')
            opts_no_profile.add_argument('--disable-dev-shm-usage')
            opts_no_profile.add_argument('--start-maximized')
            driver = _try_start(opts_no_profile)

        # 3차 시도: webdriver_manager (네트워크 필요)
        if driver is None:
            try:
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                svc = ES(EdgeChromiumDriverManager().install())
                driver = webdriver.Edge(service=svc, options=opts)
            except (ImportError, Exception) as e: # webdriver_manager가 없거나 네트워크 오류
                last_err = str(e)

        if driver is None:
            self.after(0, self._show_edgedriver_guide)
            raise RuntimeError(
                f'msedgedriver를 찾을 수 없습니다.\n'
                f'설치 안내 창을 확인해주세요.\n({last_err})'
            )

        # ── navigator.webdriver 숨김 (Cloudflare JS 감지 우회) ───────────
        stealth_js = """
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins',   {get: () => [1,2,3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR','ko','en-US','en']});
            window.chrome = {runtime: {}};
        """
        driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': stealth_js})
        driver.get(url)
        return driver

    def _show_edgedriver_guide(self):
        """msedgedriver 설치 안내 팝업."""
        import webbrowser

        # Edge 버전 자동 감지
        edge_ver = '알 수 없음'
        try:
            import subprocess, re
            out = subprocess.check_output(
                r'reg query "HKEY_CURRENT_USER\SOFTWARE\Microsoft\Edge\BLBeacon" /v version',
                shell=True, stderr=subprocess.DEVNULL
            ).decode()
            m = re.search(r'(\d+\.\d+\.\d+\.\d+)', out)
            if m:
                edge_ver = m.group(1)
        except Exception:
            pass

        guide = tk.Toplevel(self)
        guide.title('Edge WebDriver 설치 안내')
        guide.configure(bg=BG)
        guide.resizable(False, False)
        guide.grab_set()

        tk.Label(guide, text='Edge WebDriver 설치가 필요합니다',
                 bg=BG, fg=TEXT,
                 font=(FONT_MAIN, 13, 'bold')).pack(pady=(24, 6), padx=24)

        tk.Label(guide,
                 text=f'감지된 Edge 버전:  {edge_ver}',
                 bg=BG, fg=MUTED,
                 font=(FONT_MAIN, 10)).pack(pady=(0, 16), padx=24)

        script_dir = str(Path(__file__).parent)
        steps = [
            ('1', '아래 버튼을 눌러 Microsoft Edge WebDriver 다운로드 페이지를 여세요.'),
            ('2', f'현재 Edge 버전({edge_ver})과 동일한 버전의 드라이버를 다운로드하세요.'),
            ('3', '다운받은 msedgedriver.exe를 아래 위치 중 한 곳에 저장하세요:'),
            ('',  f'   ✅ 가장 쉬운 방법: {script_dir}\\  (프로그램과 같은 폴더)'), # type: ignore
            ('',  '   • C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\'), # type: ignore
            ('',  '   • 또는 사용자 다운로드 폴더 (~/Downloads/)'),
            ('4', '저장 후 이 프로그램을 재실행하고 로그인 버튼을 다시 눌러주세요.'),
        ]

        for num, text in steps:
            row = tk.Frame(guide, bg=BG)
            row.pack(fill='x', padx=24, pady=2, anchor='w')
            if num:
                tk.Label(row, text=f' {num} ', bg=ACCENT, fg=TEXT,
                         font=(FONT_MAIN, 9, 'bold'), width=2).pack(side='left', padx=(0, 8))
            tk.Label(row, text=text, bg=BG, fg=TEXT,
                     font=(FONT_MAIN, 9), justify='left', anchor='w').pack(side='left', fill='x')

        btn_row = tk.Frame(guide, bg=BG)
        btn_row.pack(pady=(20, 24), padx=24, fill='x')

        tk.Button(btn_row, text='다운로드 페이지 열기',
                  command=lambda: webbrowser.open('https://developer.microsoft.com/ko-kr/microsoft-edge/tools/webdriver/'),
                  bg=ACCENT, fg=TEXT, activebackground=ACCENT, activeforeground=TEXT,
                  font=(FONT_MAIN, 10, 'bold'),
                  relief='flat', cursor='hand2', padx=16, pady=8, bd=0
                  ).pack(side='left', padx=(0, 8))

        tk.Button(btn_row, text='닫기', command=guide.destroy,
                  bg=INPUT, fg=MUTED, activebackground=INPUT, activeforeground=MUTED,
                  font=(FONT_MAIN, 10),
                  relief='flat', cursor='hand2', padx=16, pady=8, bd=0
                  ).pack(side='left')

    # ── 핸들러 ───────────────────────────────────────────────────────────────

    def _load_urls_from_file(self):
        """파일 대화상자를 열어 .txt 파일에서 URL을 읽어와 텍스트 영역에 추가합니다."""
        filepath = filedialog.askopenfilename(
            title="URL 목록 파일 선택",
            filetypes=(("텍스트 파일", "*.txt"), ("모든 파일", "*.*")),
            initialdir=str(Path(__file__).parent)
        )
        if not filepath:
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # 기존 내용이 있으면 줄바꿈으로 구분
            current_content = self.url_text.get("1.0", "end-1c").strip()
            self.url_text.insert("end", ('\n' if current_content else '') + content)
            self._log(f"[*] 파일에서 URL을 불러왔습니다: {filepath}")
        except Exception as e:
            self._log(f"[✗] 파일 읽기 오류: {e}")
            messagebox.showerror("파일 오류", f"파일을 읽는 중 오류가 발생했습니다:\n{e}")

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d:
            self.dir_var.set(d)
            self._save_config()

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0','end')
        self.log_text.configure(state='disabled')

    def _get_log_tag(self, msg: str) -> str:
        """로그 메시지 내용에 따라 적절한 태그를 반환합니다."""
        if msg.startswith('[✗]') or 'error' in msg.lower():
            return 'error'
        if msg.startswith(('[✓]', '[*]')) or '완료' in msg:
            return 'success'
        if '[WARN]' in msg or '프록시' in msg or '건너뜀' in msg or '[!]' in msg:
            return 'warn'
        if msg.startswith(('    ', '  [')):
            return 'sub'
        return 'info'

    def _log(self, msg):
        def _w():
            self.log_text.configure(state='normal')
            self.log_text.insert('end', msg + '\n', self._get_log_tag(msg))
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.after(0, _w)

    def _set_progress(self, cur, tot):
        def _u():
            # self.prog_var.set((cur / tot * 100) if tot else 0)
            # self.prog_label.configure(text=f'{cur} / {tot} 이미지' if tot else '')
            if not tot or cur >= tot:
                self.total_eta_label.configure(text='') # 이미지 다운로드 완료 시 ETA 제거
        self.after(0, _u)

    @staticmethod
    def _fmt_eta(eta_s):
        """ETA 초 → 인간 읽기 좋은 문자열."""
        eta_s = max(0, eta_s)
        if eta_s >= 3600:
            h = int(eta_s // 3600)
            m = int((eta_s % 3600) // 60)
            return f'{h}시간 {m}분'
        elif eta_s >= 60:
            m = int(eta_s // 60)
            s = int(eta_s % 60)
            return f'{m}분 {s}초'
        else:
            return f'{int(eta_s)}초'

    def _set_total_eta(self, eta_s):
        """''전체' 행에 표시되는 전체 다운로드 완료 예상 시간 업데이트."""
        def _u():
            if eta_s > 0:
                self.total_eta_label.configure(text=f'전체 {self._fmt_eta(eta_s)} 남음')
            else:
                self.total_eta_label.configure(text='')
        self.after(0, _u)

    def _set_img_progress(self, downloaded, total_b, speed_bps):
        """1개 이미지 내부 진행 업데이트 (bytes, bytes, bytes/s)."""
        def _u():
            if downloaded == 0 and total_b == 0:
                self.sub_prog_var.set(0)
                self.sub_prog_label.configure(text='')
                self.speed_label.configure(text='')
                self.eta_label.configure(text='')
                return

            if total_b > 0:
                self.sub_prog_var.set(min(downloaded / total_b * 100, 100))
                self.sub_prog_label.configure(
                    text=f'{downloaded // 1024:,} / {total_b // 1024:,} KB')
            else:
                self.sub_prog_var.set(0)
                self.sub_prog_label.configure(text=f'{downloaded // 1024:,} KB')

            # 속도
            if speed_bps >= 1_048_576:
                spd_txt = f'{speed_bps / 1_048_576:.1f} MB/s'
            elif speed_bps >= 1024:
                spd_txt = f'{speed_bps / 1024:.0f} KB/s'
            else:
                spd_txt = f'{int(speed_bps)} B/s'
            self.speed_label.configure(text=f'{spd_txt}')

            # 이미지 1개 ETA
            if speed_bps > 0 and total_b > 0:
                self.eta_label.configure(
                    text=f'{self._fmt_eta((total_b - downloaded) / speed_bps)} 남음')
            else:
                self.eta_label.configure(text='')
        self.after(0, _u)

    def _set_dl(self, on):
        self._downloading = on
        if on:
            self.dl_btn.configure(text='다운로드 중...', state='disabled',
                                  bg=BORDER, cursor='watch')
            self.pause_btn.configure(state='normal', text='일시정지')
            self.stop_btn.configure(state='normal')
        else:
            self.dl_btn.configure(text='다운로드 시작', state='normal',
                                  bg=ACCENT, cursor='hand2')
            self.pause_btn.configure(state='disabled', text='일시정지')
            self.stop_btn.configure(state='disabled')
            # 서브 프로그레스 정리
            self.sub_prog_var.set(0)
            self.sub_prog_label.configure(text='')
            self.speed_label.configure(text='')
            self.eta_label.configure(text='')

    def _toggle_pause(self):
        if self._pause_event.is_set():
            # 일시정지 해제 → 계속다운
            self._pause_event.clear()
            self.pause_btn.configure(text='일시정지', bg=WARN, fg=INPUT)
            self._log('[*] 다운로드 계속다운')
        else:
            # 일시정지
            self._pause_event.set()
            self.pause_btn.configure(text='계속다운', bg=SUCCESS, fg=INPUT)
            self._log('[*] 다운로드 일시정지')

    def _stop_download(self):
        if messagebox.askyesno('중지 확인', '다운로드를 중지하시겠습니까?\n(현재 작업 중인 이미지는 삭제됩니다)'):
            self._pause_event.clear()  # 일시정지 상태면 해제 후 중지
            self._stop_event.set()
            self._log('[*] 중지 요청 전송...')

    def _on_closing(self):
        """프로그램 종료 시 설정 저장."""
        self._save_config()
        self.destroy()


    # ── 다운로드 ─────────────────────────────────────────────────────────────

    def _start_download(self):
        if self._downloading: return

        raw_urls_text = self.url_text.get("1.0", "end-1c")
        urls = re.split(r'[\s,]+', raw_urls_text)

        urls = [u.strip() for u in urls if u.strip()]
        out        = self.dir_var.get().strip()
        cookie_str = self.cookie_var.get()
        download_original = self.orig_img_var.get()
        add_id_to_filename = self.add_id_var.get()

        if not urls:
            messagebox.showwarning('입력 필요', 'URL을 하나 이상 입력해주세요.'); return
        bad = [u for u in urls if not u.startswith(('http://','https://'))]
        if bad:
            messagebox.showwarning('URL 오류','잘못된 URL:\n'+'\n'.join(bad)); return
        if not out:
            messagebox.showwarning('입력 필요','저장 위치를 선택해주세요.'); return

        self._clear_log()
        self.prog_var.set(0) # 전체 진행률 초기화
        self.prog_label.configure(text='')
        if cookie_str:
            self._log(f'[*] 쿠키 인증 활성화 ({len(cookie_str)}자)')

        # 이벤트 초기화
        self._stop_event.clear()
        self._pause_event.clear()
        self._set_dl(True)

        total_n  = len(urls)
        done_n   = [0]
        error_n  = [0]

        def _done(path, dl, tot):
            done_n[0] += 1
            self.after(0, lambda: self.prog_var.set(done_n[0] / total_n * 100))
            self.after(0, lambda: self.prog_label.configure(text=f'{done_n[0]} / {total_n} 완료'))
            self._log(f'[✓] ({done_n[0]}/{total_n}) 저장 완료 → {path}')
            self._log(f'    이미지: {dl} / {tot} 장')
            _check()

        def _err(msg):
            error_n[0] += 1; done_n[0] += 1
            self.after(0, lambda: self.prog_var.set(done_n[0] / total_n * 100))
            self.after(0, lambda: self.prog_label.configure(text=f'{done_n[0]} / {total_n} 완료'))
            self._log(f'[✗] 오류: {msg}')
            _check()

        def _check():
            if done_n[0] >= total_n:
                self.after(0, lambda: self.prog_label.configure(text=f'총 {total_n}개 완료'))
                self.after(0, lambda: self.total_eta_label.configure(text='')) # 전체 ETA 제거
                def _final():
                    self._set_dl(False)
                    if error_n[0] == 0:
                        messagebox.showinfo('완료', f'{total_n}개 URL 모두 저장 완료!')
                    else:
                        messagebox.showwarning('완료(일부 오류)',
                            f'{total_n}개 중 {total_n-error_n[0]}개 성공, {error_n[0]}개 실패')
                    
                    # 시스템 알림
                    if self.notify_var.get():
                        msg = (f'{total_n}개 URL 모두 저장 완료!' if error_n[0] == 0
                               else f'{total_n-error_n[0]} / {total_n}개 성공')
                        try:
                            from plyer import notification
                            notification.notify(
                                title='아카라이브 다운로드 완료',
                                message=msg,
                                app_name='아카라이브 다운로더',
                                timeout=10
                            )
                        except ImportError:
                            self._log('[!] 알림 라이브러리(plyer)가 없어 시스템 알림을 표시할 수 없습니다.')
                            self._log('    터미널: pip install plyer')
                        except Exception as e:
                            self._log(f'[✗] 시스템 알림 표시 실패: {e}')
                self.after(0, _final)

        def _run():
            self.after(0, lambda: self.prog_label.configure(text=f'0 / {total_n} 완료'))
            self.after(0, lambda: self.prog_var.set(0))

            # 파일 존재 여부 선확인 (ID 기반)
            if add_id_to_filename:
                initial_urls = []
                for url in urls:
                    article_id = url.split('/')[-1].split('?')[0].split('#')[0]
                    if article_id.isdigit():
                        # 파일명 패턴을 정확히 알 수 없으므로, ID만으로 건너뛰기
                        # glob을 사용하여 `제목 [ID].zip` 형태의 파일 확인
                        if list(Path(out).glob(f'* [{article_id}].zip')):
                            self._log(f'[!] 파일이 이미 존재하여 건너뜁니다: {url}')
                            done_n[0] += 1
                            continue
                    initial_urls.append(url)
                urls = initial_urls

            for url in urls:
                # 0. 중지 신호 확인
                if self._stop_event.is_set():
                    break
                self._log(f'\n── {url}')

                # 1. 게시글 정보 미리 가져오기 (세션 생성 포함)
                title, soup, session = _get_article_info(url, cookie_str, self._log, _err)
                if not title or not soup or not session:
                    continue # 실패 시 다음 URL로

                # 1.5. 파일 존재 여부 미리 확인
                filename = sanitize_filename(title)
                if add_id_to_filename:
                    article_id = url.split('/')[-1].split('?')[0].split('#')[0]
                    if article_id.isdigit():
                        filename += f' [{article_id}]'
                zip_path = Path(out) / f'{filename}.zip'
                if zip_path.exists():
                    done_n[0] += 1
                    self.after(0, lambda: self.prog_var.set(done_n[0] / total_n * 100))
                    self.after(0, lambda: self.prog_label.configure(text=f'{done_n[0]} / {total_n} 완료'))
                    self._log(f'[!] ({done_n[0]}/{total_n}) 이미 파일이 존재하여 건너뜁니다.')
                    self._log(f'    → {zip_path}')
                    _check()
                    continue

                # 2. 다운로드 실행 (가져온 정보와 세션 전달)
                download_article(
                    url, out, self._log, self._set_progress,
                    _done, _err, session, title, soup,
                    cookie_str=cookie_str,
                    download_original=download_original,
                    add_id_to_filename=add_id_to_filename,
                    set_img_progress=self._set_img_progress,
                    set_total_eta=self._set_total_eta,
                    stop_event=self._stop_event,
                    pause_event=self._pause_event,
                )

            # 루프 종료 후 완료 처리 (stop으로 조기 종료 시)
            if self._stop_event.is_set() and done_n[0] < total_n:
                self.after(0, lambda: self._set_dl(False))

        threading.Thread(target=_run, daemon=True).start()


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    App().mainloop()
