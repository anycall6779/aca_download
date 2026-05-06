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

def _make_session(base_url):
    s = requests.Session()
    s.headers.update({**DEFAULT_HEADERS, 'Referer': base_url})
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

def download_article(url, output_dir, log, set_progress, on_done, on_error):
    try:
        session = _make_session(url)
        log(f'[*] 요청: {url}')
        resp = session.get(url, timeout=10); resp.raise_for_status()
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

        # ── 저장 위치 ─────────────────────────────────────────────────────
        self._section_label(outer, '저장 위치')

        dir_card = tk.Frame(outer, bg=self.CARD,
                            highlightbackground=self.BORDER, highlightthickness=1)
        dir_card.pack(fill='x', pady=(4,20))

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

        urls = [v.get().strip() for _,v,_ in self._url_rows if v.get().strip()]
        out  = self.dir_var.get().strip()

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
                download_article(url, out, self._log, self._set_progress, _done, _err)

        threading.Thread(target=_run, daemon=True).start()


# ── 진입점 ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    App().mainloop()
