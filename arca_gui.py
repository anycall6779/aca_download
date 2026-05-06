"""
arca_gui.py — 아카라이브 게시글 ZIP 저장기 (GUI, 다중 URL)
"""

import io, re, copy, zipfile, threading, base64, os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
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
MAX_WORKERS = 10
ICON_PATH   = Path(__file__).parent / 'arca_icon.png'

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
    retry = Retry(total=2, backoff_factor=0,
                  status_forcelist={429,500,502,503,504},
                  allowed_methods={'GET'}, raise_on_status=False)
    adp = HTTPAdapter(max_retries=retry,
                      pool_connections=MAX_WORKERS,
                      pool_maxsize=MAX_WORKERS*2)
    s.mount('http://', adp); s.mount('https://', adp)
    return s

def sanitize_filename(name, max_len=80):
    name = re.sub(r'[\\/:*?"<>|]+',' ',name).strip()
    return re.sub(r'\s+','-',name)[:max_len] or 'post'

def get_image_ext(src):
    try:
        ext = urlparse(src).path.rsplit('.',1)[-1].lower().split('?')[0].split('#')[0]
        if ext in IMAGE_EXTS: return ext
    except: pass
    return 'png'

def escape_html(s):
    return s.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('"','&quot;')

def fetch_image(session, src, log):
    try:
        r = session.get(src, timeout=10); r.raise_for_status(); return r.content
    except Exception as e:
        parsed = urlparse(src)
        if parsed.hostname and parsed.hostname.endswith('namu.la'):
            try:
                proxy = ('https://images.weserv.nl/?url='
                         + parsed.hostname + parsed.path
                         + (('?'+parsed.query) if parsed.query else ''))
                r2 = session.get(proxy, timeout=10); r2.raise_for_status()
                log('    (namu.la → weserv.nl 프록시 성공)'); return r2.content
            except Exception as e2:
                log(f'    [WARN] 프록시 실패: {e2}')
        else:
            log(f'    [WARN] 실패: {e}')
        return None

def download_article(url, output_dir, log, set_progress, on_done, on_error, cookie_str=''):
    try:
        session = _make_session(url, cookie_str=cookie_str)
        log(f'[*] 요청: {url}')
        if cookie_str.strip():
            log('    (쿠키 인증 사용 중)')
        resp = session.get(url, timeout=10)
        if resp.status_code == 451:
            on_error(f'HTTP 451: 법적 사유로 차단된 페이지입니다.\n'
                     f'유효한 아카라이브 로그인 쿠키를 입력해야 접근할 수 있습니다.')
            return
        resp.raise_for_status()
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'lxml')

        og = soup.find('meta', property='og:title')
        T  = (og.get('content','') if og else '') or (soup.title.string if soup.title else '') or 'post'
        T  = T.strip()

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
            if not img.get('src') and img.get('data-src'):
                img['src'] = img['data-src']

        imgs  = [i for i in content.find_all('img') if i.get('src')]
        total = len(imgs)
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

        def _dl(task):
            nonlocal done_n
            idx, img, src, name = task
            log(f'  [{idx:03d}/{total}] {src}')
            data = fetch_image(session, src, log)
            if not data: log('       → 건너뜀')
            with lock:
                done_n += 1; set_progress(done_n, total)
            return idx, name, data

        zip_buf = io.BytesIO(); downloaded = []
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

        out_path = Path(output_dir) / f'arca-{sanitize_filename(T)}.zip'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(zip_buf.getvalue())
        on_done(str(out_path.resolve()), len(downloaded), total)

    except Exception as e:
        on_error(str(e))


# ── GUI ───────────────────────────────────────────────────────────────────────

class App(tk.Tk):
    # 팔레트 (밝고 깔끔한 다크 테마)
    BG      = '#13151f'
    PANEL   = '#1c1f2e'
    CARD    = '#252839'
    INPUT   = '#1a1d2a'
    ACCENT  = '#5c7cfa'
    ACCENT2 = '#748ffc'
    TEXT    = '#e9ecf5'
    MUTED   = '#8890b0'
    SUCCESS = '#51cf66'
    WARN    = '#fcc419'
    ERROR   = '#ff6b6b'
    BORDER  = '#2e3350'
    ADD_C   = '#20c997'
    DEL_C   = '#fa5252'
    SEP     = '#23263a'

    def __init__(self):
        super().__init__()
        self.title('아카라이브 다운로더')
        self.geometry('780x720')
        self.minsize(640, 560)
        self.configure(bg=self.BG)
        self.resizable(True, True)

        # 아카라이브 아이콘 설정
        if ICON_PATH.exists():
            try:
                from PIL import Image, ImageTk
                img = Image.open(ICON_PATH).resize((32,32), Image.LANCZOS)
                self._icon = ImageTk.PhotoImage(img)
                self.iconphoto(True, self._icon)
            except Exception:
                pass

        self._url_rows: list[tuple] = []
        self._url_container = None
        self._downloading   = False

        self._build_styles()
        self._build_ui()

    # ── 스타일 ───────────────────────────────────────────────────────────────

    def _build_styles(self):
        s = ttk.Style(self)
        s.theme_use('clam')
        s.configure('TFrame',       background=self.BG)
        s.configure('Panel.TFrame', background=self.PANEL)
        s.configure('TLabel',       background=self.BG,    foreground=self.TEXT,  font=('Segoe UI',10))
        s.configure('H1.TLabel',    background=self.BG,    foreground=self.TEXT,  font=('Segoe UI',17,'bold'))
        s.configure('Muted.TLabel', background=self.BG,    foreground=self.MUTED, font=('Segoe UI',9))
        s.configure('TProgressbar', troughcolor=self.PANEL, background=self.ACCENT, thickness=4, borderwidth=0)

    # ── UI ───────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = tk.Frame(self, bg=self.BG)
        outer.pack(fill='both', expand=True, padx=28, pady=24)

        # ── 헤더 ─────────────────────────────────────────────────────────
        hdr = tk.Frame(outer, bg=self.BG)
        hdr.pack(fill='x', pady=(0,20))

        # 아이콘 + 제목
        if ICON_PATH.exists():
            try:
                from PIL import Image, ImageTk
                ico = Image.open(ICON_PATH).resize((36,36), Image.LANCZOS)
                self._hdr_icon = ImageTk.PhotoImage(ico)
                tk.Label(hdr, image=self._hdr_icon, bg=self.BG).pack(side='left', padx=(0,10))
            except: pass

        title_col = tk.Frame(hdr, bg=self.BG)
        title_col.pack(side='left')
        tk.Label(title_col, text='아카라이브 다운로더',
                 bg=self.BG, fg=self.TEXT, font=('Segoe UI',17,'bold')).pack(anchor='w')
        tk.Label(title_col, text='게시글 URL을 입력하면 이미지 포함 ZIP으로 저장합니다',
                 bg=self.BG, fg=self.MUTED, font=('Segoe UI',9)).pack(anchor='w')

        # ── URL 섹션 ──────────────────────────────────────────────────────
        self._section_label(outer, 'URL 목록')

        url_card = tk.Frame(outer, bg=self.CARD,
                            highlightbackground=self.BORDER, highlightthickness=1)
        url_card.pack(fill='x', pady=(4,16))

        top_row = tk.Frame(url_card, bg=self.CARD)
        top_row.pack(fill='x', padx=14, pady=(12,6))
        tk.Label(top_row, text='다운로드할 게시글 URL을 입력하세요',
                 bg=self.CARD, fg=self.MUTED, font=('Segoe UI',9)).pack(side='left')
        self._btn(top_row, '＋  URL 추가', self._add_url_row,
                  bg=self.ADD_C, side='right')

        # URL 행 컨테이너
        self._url_container = tk.Frame(url_card, bg=self.CARD)
        self._url_container.pack(fill='x', padx=14, pady=(0,12))
        self._add_url_row()   # 첫 행

        # ── 엄카라이브 로그인 섹션 ──────────────────────────────────
        self._section_label(outer, '아카라이브 로그인 (선택 — HTTP 451 차단 우회)')

        cookie_card = tk.Frame(outer, bg=self.CARD,
                               highlightbackground=self.BORDER, highlightthickness=1)
        cookie_card.pack(fill='x', pady=(4,16))

        ck_row = tk.Frame(cookie_card, bg=self.CARD)
        ck_row.pack(fill='x', padx=14, pady=14)

        # 상태 표시 라벨
        self.login_status_var = tk.StringVar(value='⚪  미로그인')
        self.login_status_lbl = tk.Label(
            ck_row, textvariable=self.login_status_var,
            bg=self.CARD, fg=self.MUTED,
            font=('Segoe UI', 10, 'bold'), width=22, anchor='w'
        )
        self.login_status_lbl.pack(side='left')

        # 로그인 버튼
        self.login_btn = tk.Button(
            ck_row, text='🔑  아카라이브 로그인',
            command=self._do_login,
            bg=self.ACCENT, fg='#ffffff',
            activebackground=self.ACCENT2, activeforeground='#ffffff',
            font=('Segoe UI', 10, 'bold'),
            relief='flat', cursor='hand2', padx=14, pady=6, bd=0
        )
        self.login_btn.pack(side='left', padx=(0, 8))

        # 상태 초기화 버튼
        tk.Button(
            ck_row, text='삭제',
            command=self._clear_login,
            bg=self.INPUT, fg=self.MUTED,
            activebackground=self.BORDER, activeforeground=self.TEXT,
            font=('Segoe UI', 9),
            relief='flat', cursor='hand2', padx=10, pady=6, bd=0
        ).pack(side='left')

        self.cookie_var = tk.StringVar()  # 내부 쿠키 저장용 (UI에 직접 표시 안 함)

        tk.Label(cookie_card,
                 text='  ℹ️  로그인 버튼을 클릭하면 브라우저 창이 열립니다. 로그인 완료 시 쿠키를 자동으로 가져옵니다.',
                 bg=self.CARD, fg=self.MUTED, font=('Segoe UI', 8),
                 justify='left'
                 ).pack(anchor='w', padx=14, pady=(0, 10))

        # ── 저장 위치 ─────────────────────────────────────────────────────
        self._section_label(outer, '저장 위치')

        dir_card = tk.Frame(outer, bg=self.CARD,
                            highlightbackground=self.BORDER, highlightthickness=1)
        dir_card.pack(fill='x', pady=(4,16))

        dir_row = tk.Frame(dir_card, bg=self.CARD)
        dir_row.pack(fill='x', padx=14, pady=12)

        self.dir_var = tk.StringVar(value=str(Path.home()/'Downloads'))
        tk.Entry(dir_row, textvariable=self.dir_var,
                 bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT,
                 font=('Segoe UI',10), relief='flat',
                 highlightbackground=self.BORDER, highlightthickness=1
                 ).pack(side='left', fill='x', expand=True, ipady=8, padx=(0,8))
        self._btn(dir_row, '📂  폴더 선택', self._browse_dir,
                  bg=self.ACCENT, side='left')

        # ── 다운로드 버튼 ─────────────────────────────────────────────────
        self.dl_btn = tk.Button(outer, text='⬇   다운로드 시작',
                                command=self._start_download,
                                bg=self.ACCENT, fg='#ffffff',
                                activebackground=self.ACCENT2, activeforeground='#ffffff',
                                font=('Segoe UI',12,'bold'),
                                relief='flat', cursor='hand2',
                                pady=13, padx=20, bd=0)
        self.dl_btn.pack(fill='x', pady=(0,14))

        # ── 진행바 ───────────────────────────────────────────────────────
        self.prog_var = tk.DoubleVar(value=0)
        ttk.Progressbar(outer, variable=self.prog_var,
                        maximum=100, style='TProgressbar').pack(fill='x')
        self.prog_label = tk.Label(outer, text='', bg=self.BG, fg=self.MUTED,
                                   font=('Segoe UI',9))
        self.prog_label.pack(anchor='e', pady=(3,10))

        # ── 로그 ─────────────────────────────────────────────────────────
        log_top = tk.Frame(outer, bg=self.BG)
        log_top.pack(fill='x')
        tk.Label(log_top, text='실행 로그', bg=self.BG, fg=self.MUTED,
                 font=('Segoe UI',9,'bold')).pack(side='left')
        self._btn(log_top, '지우기', self._clear_log,
                  bg=self.PANEL, fg=self.MUTED, side='right', padx=8, font_size=8)

        log_wrap = tk.Frame(outer, bg=self.INPUT,
                            highlightbackground=self.BORDER, highlightthickness=1)
        log_wrap.pack(fill='both', expand=True, pady=(4,0))
        self.log_text = tk.Text(log_wrap, bg=self.INPUT, fg=self.TEXT,
                                font=('Consolas',9), relief='flat',
                                wrap='word', state='disabled',
                                selectbackground=self.BORDER,
                                insertbackground=self.TEXT, padx=12, pady=10)
        self.log_text.pack(side='left', fill='both', expand=True)
        sb = ttk.Scrollbar(log_wrap, command=self.log_text.yview)
        sb.pack(side='right', fill='y')
        self.log_text['yscrollcommand'] = sb.set

        self.log_text.tag_configure('info',    foreground=self.TEXT)
        self.log_text.tag_configure('warn',    foreground=self.WARN)
        self.log_text.tag_configure('success', foreground=self.SUCCESS)
        self.log_text.tag_configure('error',   foreground=self.ERROR)
        self.log_text.tag_configure('sub',     foreground=self.MUTED)

    # ── 헬퍼 ─────────────────────────────────────────────────────────────────

    def _section_label(self, parent, text):
        f = tk.Frame(parent, bg=self.BG)
        f.pack(fill='x', pady=(0,2))
        tk.Label(f, text=text, bg=self.BG, fg=self.MUTED,
                 font=('Segoe UI',9,'bold')).pack(side='left')
        tk.Frame(f, bg=self.SEP, height=1).pack(side='left', fill='x', expand=True, padx=(8,0), pady=6)

    def _btn(self, parent, text, cmd, bg=None, fg='#ffffff',
             side='left', padx=10, font_size=9):
        bg = bg or self.ACCENT
        tk.Button(parent, text=text, command=cmd,
                  bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                  font=('Segoe UI', font_size, 'bold'),
                  relief='flat', cursor='hand2', padx=padx, pady=5, bd=0
                  ).pack(side=side)

    # ── URL 행 ───────────────────────────────────────────────────────────────

    def _add_url_row(self):
        idx = len(self._url_rows)
        row = tk.Frame(self._url_container, bg=self.CARD)
        row.pack(fill='x', pady=(0,5))

        var = tk.StringVar()
        ent = tk.Entry(row, textvariable=var,
                       bg=self.INPUT, fg=self.TEXT, insertbackground=self.TEXT,
                       font=('Segoe UI',11), relief='flat',
                       highlightbackground=self.BORDER, highlightthickness=1)
        ent.pack(side='left', fill='x', expand=True, ipady=8, padx=(0,6))
        ent.bind('<Return>', lambda e: self._start_download())

        tk.Button(row, text='붙여넣기',
                  command=lambda v=var: v.set(self._clip()),
                  bg=self.INPUT, fg=self.MUTED, activebackground=self.BORDER,
                  activeforeground=self.TEXT, font=('Segoe UI',9),
                  relief='flat', cursor='hand2', padx=8, bd=0
                  ).pack(side='left', padx=(0,4))

        rec = (row, var, ent)
        del_cfg = dict(bg=self.CARD, fg=self.DEL_C, activebackground=self.BORDER,
                       activeforeground=self.DEL_C, font=('Segoe UI',11,'bold'),
                       relief='flat', cursor='hand2', padx=6, bd=0)
        if idx == 0:
            del_cfg.update(fg=self.BORDER, cursor='arrow')
            tk.Button(row, text='✕', state='disabled', **del_cfg).pack(side='left')
        else:
            tk.Button(row, text='✕',
                      command=lambda r=row, rc=rec: self._remove_url_row(r, rc),
                      **del_cfg).pack(side='left')

        self._url_rows.append(rec)

    def _remove_url_row(self, frame, rec):
        if len(self._url_rows) <= 1: return
        self._url_rows.remove(rec)
        frame.destroy()

    def _clip(self):
        try: return self.clipboard_get().strip()
        except: return ''

    def _set_login_status(self, text, color):
        def _u():
            self.login_status_var.set(text)
            self.login_status_lbl.configure(fg=color)
        self.after(0, _u)

    def _clear_login(self):
        self.cookie_var.set('')
        self._set_login_status('⚪  미로그인', self.MUTED)
        self._log('[*] 로그인 정보 삭제됨')

    def _do_login(self):
        """Selenium 브라우저를 열어 로그인 후 쿠키 자동 수집."""
        if self.cookie_var.get():
            if not messagebox.askyesno('재로그인', '이미 로그인되어 있습니다.\n다시 로그인하시겠습니까?'):
                return

        self.login_btn.configure(state='disabled', text='브라우저 열는 중...')
        self._set_login_status('⏳  로그인 대기 중', '#fbbf24')
        self._log('[*] 아카라이브 로그인 브라우저 열기 중...')

        def _run():
            try:
                driver = self._open_browser('https://arca.live/u/login')
            except RuntimeError as e:
                _e = str(e)
                self.after(0, lambda msg=_e: (
                    self._set_login_status('❌  브라우저 오류', self.ERROR),
                    self._log(f'[✗] {msg}'),
                    self.login_btn.configure(state='normal', text='🔑  아카라이브 로그인')
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
                            self._set_login_status(f'✅  로그인됨 ({n}개)', self.SUCCESS)
                            self._log(f'[✓] 쿠키 {n}개 수집 완료 — 이제 다운로드하세요!')
                            self.login_btn.configure(state='normal', text='🔑  다시 로그인')
                        self.after(0, _ok)
                        time.sleep(2)
                        break
                else:
                    self.after(0, lambda: (
                        self._set_login_status('⏰  시간 초과', self.ERROR),
                        self._log('[⚠] 6분 내 로그인 감지 실패. 다시 시도해주세요.'),
                        self.login_btn.configure(state='normal', text='🔑  아카라이브 로그인')
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
            except Exception as e:
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
        guide.configure(bg=self.BG)
        guide.resizable(False, False)
        guide.grab_set()

        tk.Label(guide, text='⚙️  Edge WebDriver 설치가 필요합니다',
                 bg=self.BG, fg=self.TEXT,
                 font=('Segoe UI', 13, 'bold')).pack(pady=(24, 6), padx=24)

        tk.Label(guide,
                 text=f'감지된 Edge 버전:  {edge_ver}',
                 bg=self.BG, fg=self.MUTED,
                 font=('Segoe UI', 10)).pack(pady=(0, 16), padx=24)

        script_dir = str(Path(__file__).parent)
        steps = [
            ('1', '아래 버튼을 눌러 Microsoft Edge WebDriver 다운로드 페이지를 여세요.'),
            ('2', f'현재 Edge 버전({edge_ver})과 동일한 버전의 드라이버를 다운로드하세요.'),
            ('3', '다운받은 msedgedriver.exe를 아래 위치 중 한 곳에 저장하세요:'),
            ('',  f'   ✅ 가장 쉬운 방법: {script_dir}\\  (프로그램과 같은 폴더)'),
            ('',  '   • C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\'),
            ('',  '   • 또는 사용자 다운로드 폴더 (~/Downloads/)'),
            ('4', '저장 후 이 프로그램을 재실행하고 로그인 버튼을 다시 눌러주세요.'),
        ]

        for num, text in steps:
            row = tk.Frame(guide, bg=self.BG)
            row.pack(fill='x', padx=24, pady=2, anchor='w')
            if num:
                tk.Label(row, text=f' {num} ', bg=self.ACCENT, fg='#fff',
                         font=('Segoe UI', 9, 'bold'), width=2).pack(side='left', padx=(0, 8))
            tk.Label(row, text=text, bg=self.BG, fg=self.TEXT,
                     font=('Segoe UI', 9), justify='left', anchor='w').pack(side='left', fill='x')

        btn_row = tk.Frame(guide, bg=self.BG)
        btn_row.pack(pady=(20, 24), padx=24, fill='x')

        tk.Button(btn_row, text='🌐  다운로드 페이지 열기',
                  command=lambda: webbrowser.open('https://developer.microsoft.com/en-us/microsoft-edge/tools/webdriver/'),
                  bg=self.ACCENT, fg='#fff', activebackground=self.ACCENT2,
                  font=('Segoe UI', 10, 'bold'),
                  relief='flat', cursor='hand2', padx=16, pady=8, bd=0
                  ).pack(side='left', padx=(0, 8))

        tk.Button(btn_row, text='닫기', command=guide.destroy,
                  bg=self.INPUT, fg=self.MUTED,
                  font=('Segoe UI', 10),
                  relief='flat', cursor='hand2', padx=16, pady=8, bd=0
                  ).pack(side='left')

    # ── 핸들러 ───────────────────────────────────────────────────────────────

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get())
        if d: self.dir_var.set(d)

    def _clear_log(self):
        self.log_text.configure(state='normal')
        self.log_text.delete('1.0','end')
        self.log_text.configure(state='disabled')

    def _log(self, msg):
        def _w():
            self.log_text.configure(state='normal')
            tag = ('warn'    if '[WARN]' in msg or '프록시' in msg or '건너뜀' in msg else
                   'success' if msg.startswith('[✓]') or '완료' in msg else
                   'error'   if msg.startswith('[✗]') or 'error' in msg.lower() else
                   'sub'     if msg.startswith(('    ','  [')) else 'info')
            self.log_text.insert('end', msg+'\n', tag)
            self.log_text.see('end')
            self.log_text.configure(state='disabled')
        self.after(0, _w)

    def _set_progress(self, cur, tot):
        def _u():
            self.prog_var.set((cur/tot*100) if tot else 0)
            self.prog_label.configure(text=f'{cur} / {tot} 이미지' if tot else '')
        self.after(0, _u)

    def _set_dl(self, on):
        self._downloading = on
        if on:
            self.dl_btn.configure(text='⏳  다운로드 중...', state='disabled',
                                  bg='#2e3350', cursor='watch')
        else:
            self.dl_btn.configure(text='⬇   다운로드 시작', state='normal',
                                  bg=self.ACCENT, cursor='hand2')

    # ── 다운로드 ─────────────────────────────────────────────────────────────

    def _start_download(self):
        if self._downloading: return

        urls       = [v.get().strip() for _,v,_ in self._url_rows if v.get().strip()]
        out        = self.dir_var.get().strip()
        cookie_str = self.cookie_var.get().strip()

        if not urls:
            messagebox.showwarning('입력 필요','URL을 하나 이상 입력해주세요.'); return
        bad = [u for u in urls if not u.startswith(('http://','https://'))]
        if bad:
            messagebox.showwarning('URL 오류','잘못된 URL:\n'+'\n'.join(bad)); return
        if not out:
            messagebox.showwarning('입력 필요','저장 위치를 선택해주세요.'); return

        self._clear_log()
        self._set_progress(0,0)
        self.prog_label.configure(text='')
        if cookie_str:
            self._log(f'[*] 쿠키 인증 활성화 ({len(cookie_str)}자)')
        self._set_dl(True)

        total_n  = len(urls)
        done_n   = [0]
        error_n  = [0]

        def _done(path, dl, tot):
            done_n[0] += 1
            self._log(f'[✓] ({done_n[0]}/{total_n}) 저장 완료 → {path}')
            self._log(f'    이미지: {dl} / {tot} 장')
            _check()

        def _err(msg):
            error_n[0] += 1; done_n[0] += 1
            self._log(f'[✗] 오류: {msg}')
            _check()

        def _check():
            if done_n[0] >= total_n:
                def _final():
                    self._set_dl(False)
                    if error_n[0] == 0:
                        messagebox.showinfo('완료', f'{total_n}개 URL 모두 저장 완료!')
                    else:
                        messagebox.showwarning('완료(일부 오류)',
                            f'{total_n}개 중 {total_n-error_n[0]}개 성공, {error_n[0]}개 실패')
                self.after(0, _final)

        def _run():
            for url in urls:
                self._log(f'\n── {url}')
                download_article(url, out, self._log, self._set_progress,
                                 _done, _err, cookie_str=cookie_str)

        threading.Thread(target=_run, daemon=True).start()


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    App().mainloop()
