from flask import Flask, render_template, request, jsonify, Response
from playwright.sync_api import sync_playwright
from playwright.async_api import async_playwright as async_pw
import threading, time, re, sys, difflib, json, base64, os, resource, asyncio
from PIL import Image, ImageOps
from io import BytesIO
from collections import Counter, deque
import numpy as np

def is_dead(e):
    """Return True if the exception means the browser/page is gone."""
    s = str(e).lower()
    t = type(e).__name__.lower()
    return ("target page" in s or "browser has been closed" in s or
            "target closed" in s or "crash" in s or "disposed" in s or
            "connection closed" in s or "browser disconnected" in s or
            "frame was detached" in s or "err_aborted" in s or
            "net::err" in s or "page crash" in s or
            "eagain" in s or "resource temporarily unavailable" in s or
            "failed to launch" in s or "spawn" in s or
            "targetclosed" in t)

_tab_prefix = threading.local()

# 
#  CONCURRENCY LIMITS
# 
CAPTCHA_CONCURRENCY = int(os.environ.get("CAPTCHA_CONCURRENCY", "1"))
_captcha_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)
_ocr_semaphore = threading.Semaphore(CAPTCHA_CONCURRENCY)
MAX_GLOBAL_BROWSERS = int(os.environ.get("MAX_GLOBAL_BROWSERS", "6"))
_browser_semaphore = threading.Semaphore(MAX_GLOBAL_BROWSERS)
_active_browsers = 0
_active_browsers_lock = threading.Lock()

# Pages per browser (shared browser mode)
# Each Chromium process handles this many Zefoy sessions
PAGES_PER_BROWSER = int(os.environ.get("PAGES_PER_BROWSER", "3"))

try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    resource.setrlimit(resource.RLIMIT_NPROC, (hard, hard))
    print(f"[SYS] Raised RLIMIT_NPROC to {hard}", flush=True)
except:
    pass
try:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    print(f"[SYS] Raised RLIMIT_NOFILE to {hard}", flush=True)
except:
    pass

app = Flask(__name__)
ZEFOY = "https://zefoy.com"

def parse_proxy(raw):
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("socks"):
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        return f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    return raw

PROXY_URL = parse_proxy(os.environ.get("PROXY_URL", ""))
USE_TOR = os.environ.get("USE_TOR", "true").strip().lower() in ("true", "1", "yes")
if not PROXY_URL and USE_TOR:
    PROXY_URL = "socks5://127.0.0.1:9050"
    USING_TOR = True
else:
    USING_TOR = False

def renew_tor_circuit():
    import socket
    try:
        cookie_path = "/tmp/tor-data/control_auth_cookie"
        if not os.path.exists(cookie_path):
            print("[TOR] No control cookie found  cannot renew circuit", flush=True)
            return False
        with open(cookie_path, "rb") as f:
            cookie = f.read()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(10)
        s.connect(("127.0.0.1", 9060))
        s.send(b"AUTHENTICATE " + cookie.hex().encode() + b"\r\n")
        resp = s.recv(256)
        if b"250" not in resp:
            print(f"[TOR] Auth failed: {resp}", flush=True)
            s.close()
            return False
        s.send(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(256)
        s.close()
        if b"250" in resp:
            print("[TOR]  New circuit requested  fresh IP incoming!", flush=True)
            time.sleep(5)
            return True
        else:
            print(f"[TOR] NEWNYM failed: {resp}", flush=True)
            return False
    except Exception as e:
        print(f"[TOR] Circuit renewal error: {e}", flush=True)
        return False

# 
#  OVERLAY REMOVAL HELPER
# 
def remove_overlays(page):
    """Strip ad iframes and consent dialogs that can intercept clicks (2026 DOM)."""
    try:
        page.evaluate("""() => {
            document.querySelectorAll('iframe').forEach(el => el.remove());
            document.querySelectorAll('.fc-dialog-overlay, .fc-monetization-dialog-container, .fc-message-root, .fc-consent-root').forEach(el => el.remove());
            document.querySelectorAll('.adsbygoogle, .ad-container, iframe[src*="googleads"], iframe[src*="ads"], iframe.adsbygoogle').forEach(el => el.remove());
            document.querySelectorAll('[style*="position: fixed"], [style*="position: absolute"]').forEach(el => {
                if (el.style.zIndex && parseInt(el.style.zIndex) > 9000) {
                    // Don't nuke captcha elements or their containers
                    if (el.querySelector('#captcha-img, input[name*="captcha"]') ||
                        el.closest('.wrapper-capth, .captcha-container, form')) return;
                    el.remove();
                }
            });
            document.querySelectorAll('button').forEach(btn => {
                if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
            });
        }""")
    except:
        pass

# 
#  ANTI-DETECTION SCRIPTS
# 
DISMISS_ALERTS_JS = "window.alert = function() { return true; }; window.confirm = function() { return true; };"

BLOCK_FC_POPUPS_JS = """(() => {
    const cleanPage = () => {
        document.querySelectorAll('iframe').forEach(el => el.remove());
        document.querySelectorAll('.fc-monetization-dialog-container, .fc-message-root, .fc-dialog-overlay, .fc-consent-root').forEach(el => el.remove());
        document.querySelectorAll('.adsbygoogle').forEach(el => el.remove());
        document.querySelectorAll('button').forEach(btn => {
            if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
        });
    };
    setTimeout(cleanPage, 800);
    const observer = new MutationObserver(cleanPage);
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
    else document.addEventListener('DOMContentLoaded', () => observer.observe(document.body, { childList: true, subtree: true }));
})();"""

MOUSE_SIMULATION_K9X_JS = """(() => {
    function generateK9xMouseData() {
        const points = [];
        const numPoints = Math.floor(Math.random() * 16) + 12;
        for (let i = 0; i < numPoints; i++) {
            const x = Math.floor(Math.random() * 1850) + 50;
            const y = Math.floor(Math.random() * 950) + 50;
            const d = (Math.random() * 2.75 + 0.05).toFixed(4);
            const g = Math.random() > 0.65 ? "True" : "False";
            points.push(`x=${x}&y=${y}&d=${d}&g=${g}`);
        }
        const raw = points.join("|");
        let xored = "";
        for (let i = 0; i < raw.length; i++) {
            xored += String.fromCharCode(raw.charCodeAt(i) ^ ((i % 5) + 77));
        }
        const wrapped = "K9x!" + xored + "K9x!";
        const encoded = btoa(wrapped);
        let reversed = encoded.split("").reverse().join("");
        while (reversed.length % 4 !== 0) reversed += "=";
        return reversed;
    }
    function injectMouseData() {
        const mouseData = generateK9xMouseData();
        document.querySelectorAll('input[type="hidden"]').forEach(input => {
            if (!input.value && input.name !== 'captcha_encoded') input.value = mouseData;
        });
        window.__zefoyMouseData = mouseData;
    }
    setTimeout(injectMouseData, 500);
    setTimeout(injectMouseData, 1500);
    setTimeout(injectMouseData, 3000);
    document.addEventListener('submit', function(e) { injectMouseData(); }, true);
    document.addEventListener('click', function(e) {
        if (e.target.tagName === 'BUTTON' || e.target.closest('button')) setTimeout(injectMouseData, 50);
    }, true);
    const observer = new MutationObserver(function(mutations) {
        mutations.forEach(function(m) { if (m.addedNodes.length > 0) setTimeout(injectMouseData, 100); });
    });
    if (document.body) observer.observe(document.body, { childList: true, subtree: true });
    else document.addEventListener('DOMContentLoaded', function() {
        observer.observe(document.body, { childList: true, subtree: true }); injectMouseData();
    });
    window.generateK9xMouseData = generateK9xMouseData;
    window.injectMouseData = injectMouseData;
})();"""

GENERATE_CF_OB_TE_JS = """(() => {
    function generateCfObTeCookie() {
        const source = "HTMLButtonElement.onclick@https://zefoy.com/:1:1";
        const kod = "DOMContentLoaded";
        const payload = `Kod: ${kod}\nsource: ${source}`;
        const cookieValue = btoa(payload);
        const expiry = new Date(Date.now() + 5 * 60 * 60 * 1000).toUTCString();
        document.cookie = `cf_ob_te=${cookieValue}; Path=/; Expires=${expiry}`;
        return cookieValue;
    }
    generateCfObTeCookie();
    setInterval(generateCfObTeCookie, 60000);
    window.generateCfObTeCookie = generateCfObTeCookie;
})();"""


async def inject_anti_detection_async(page):
    """Async version for async Playwright."""
    try:
        for script in [DISMISS_ALERTS_JS, BLOCK_FC_POPUPS_JS, MOUSE_SIMULATION_K9X_JS, GENERATE_CF_OB_TE_JS]:
            await page.evaluate(script)
    except:
        pass

async def remove_overlays_async(page):
    """Async version - strip ad iframes and consent dialogs."""
    try:
        await page.evaluate("""() => {
            document.querySelectorAll('iframe').forEach(el => el.remove());
            document.querySelectorAll('.fc-dialog-overlay, .fc-monetization-dialog-container, .fc-message-root, .fc-consent-root').forEach(el => el.remove());
            document.querySelectorAll('.adsbygoogle, .ad-container, iframe[src*="googleads"], iframe[src*="ads"], iframe.adsbygoogle').forEach(el => el.remove());
            document.querySelectorAll('[style*="position: fixed"], [style*="position: absolute"]').forEach(el => {
                if (el.style.zIndex && parseInt(el.style.zIndex) > 9000) {
                    if (el.querySelector('#captcha-img, input[name*="captcha"]') ||
                        el.closest('.wrapper-capth, .captcha-container, form')) return;
                    el.remove();
                }
            });
            document.querySelectorAll('button').forEach(btn => {
                if (btn.textContent.includes('Consent') && btn.offsetParent !== null) btn.click();
            });
        }""")
    except:
        pass
def inject_anti_detection(page):
    try:
        for script in [DISMISS_ALERTS_JS, BLOCK_FC_POPUPS_JS, MOUSE_SIMULATION_K9X_JS, GENERATE_CF_OB_TE_JS]:
            page.evaluate(script)
    except:
        pass

HEARTS_BTN_SEL = "button.wbutton.btn-dark"

# 
#  SERVICES
# 
SERVICES = {
    "hearts": {
        "name": "Hearts",
        "emoji": "",
        "button_class": "t-hearts-button",
        "menu_class": "t-hearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "views": {
        "name": "Views",
        "emoji": "",
        "button_class": "t-views-button",
        "menu_class": "t-views-menu",
        "unit": "views",
        "engine": "zefoy",
    },
    "comment_hearts": {
        "name": "Comment Hearts",
        "emoji": "",
        "button_class": "t-chearts-button",
        "menu_class": "t-chearts-menu",
        "unit": "hearts",
        "engine": "zefoy",
    },
    "shares": {
        "name": "Shares",
        "emoji": "",
        "button_class": "t-shares-button",
        "menu_class": "t-shares-menu",
        "unit": "shares",
        "engine": "zefoy",
    },
    "favorites": {
        "name": "Favorites",
        "emoji": "",
        "button_class": "t-favorites-button",
        "menu_class": "t-favorites-menu",
        "unit": "favorites",
        "engine": "zefoy",
    },
    "followers": {
        "name": "Followers",
        "emoji": "",
        "button_class": "t-followers-button",
        "menu_class": "t-followers-menu",
        "unit": "followers",
        "engine": "zefoy",
    },
}

ANY_SERVICE_BUTTON = ", ".join(f".{s['button_class']}" for s in SERVICES.values() if 'button_class' in s)

# 
#  DICTIONARY
# 
WORD_LIST = []
def load_dictionary():
    global WORD_LIST
    try:
        with open('/usr/share/dict/words') as f:
            WORD_LIST = [w.strip().lower() for w in f if 2 <= len(w.strip()) <= 10]
        print(f"[BOT] Dictionary loaded: {len(WORD_LIST)} words", flush=True)
    except:
        try:
            import urllib.request
            url = "https://raw.githubusercontent.com/dwyl/english-words/master/words_alpha.txt"
            data = urllib.request.urlopen(url, timeout=10).read().decode()
            WORD_LIST = [w.strip().lower() for w in data.splitlines() if 2 <= len(w.strip()) <= 10]
            print(f"[BOT] Online dictionary loaded: {len(WORD_LIST)} words", flush=True)
        except Exception as e:
            print(f"[BOT] Dictionary load failed: {e}", flush=True)

threading.Thread(target=load_dictionary, daemon=True).start()

# 
#  CAPTCHA SOLVER — ENHANCED
# 
import hashlib
_captcha_cache = {}  # sha256 -> answer

def remove_small_components(binary_arr, min_size=30):
    h, w = binary_arr.shape
    visited = np.zeros((h, w), dtype=bool)
    result = np.zeros((h, w), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            if binary_arr[y, x] == 1 and not visited[y, x]:
                component = []
                q = deque([(y, x)])
                visited[y, x] = True
                while q:
                    cy, cx = q.popleft()
                    component.append((cy, cx))
                    for dy, dx in [(-1,0),(1,0),(0,-1),(0,1),(-1,-1),(-1,1),(1,-1),(1,1)]:
                        ny, nx = cy + dy, cx + dx
                        if 0 <= ny < h and 0 <= nx < w and binary_arr[ny, nx] == 1 and not visited[ny, nx]:
                            visited[ny, nx] = True
                            q.append((ny, nx))
                if len(component) >= min_size:
                    for cy, cx in component:
                        result[cy, cx] = 1
    return result

def _image_hash(img_bytes):
    return hashlib.sha256(img_bytes).hexdigest()[:16]

def solve_captcha(img_bytes):
    h = _image_hash(img_bytes)
    if h in _captcha_cache:
        print(f"[BOT] Captcha cache hit: '{_captcha_cache[h]}'", flush=True)
        return _captcha_cache[h]
    with _ocr_semaphore:
        ans = _solve_captcha_inner(img_bytes)
    if ans and len(ans) >= 3:
        _captcha_cache[h] = ans
    return ans

def _levenstein_best(candidates, word_list):
    """Pick the best candidate using Levenshtein-like scoring."""
    from collections import Counter as _Cnt
    if not candidates:
        return None
    freq = _Cnt(candidates)
    word_set = set(word_list)
    exact = [c for c in candidates if c in word_set]
    if exact:
        return _Cnt(exact).most_common(1)[0][0]
    best_word = None
    best_score = 0.0
    seen = {}
    for c, f in freq.items():
        from difflib import SequenceMatcher as SM
        # Compare against all words, keep best
        for w in word_list:
            if abs(len(c) - len(w)) > 3:
                continue
            sim = SM(None, c, w).ratio()
            score = sim * f
            if score > best_score:
                best_score = score
                best_word = w
                seen[c] = w
    if best_word and best_score >= 1.2:
        return best_word
    return None

def _otsu_threshold(arr):
    """Compute Otsu threshold from numpy array."""
    hist, _ = np.histogram(arr, bins=256, range=(0, 256))
    total = arr.size
    sum_total = np.dot(np.arange(256), hist)
    sum_b = 0
    w_b = 0
    w_f = 0
    var_max = 0
    threshold = 128
    for t in range(256):
        w_b += hist[t]
        if w_b == 0:
            continue
        w_f = total - w_b
        if w_f == 0:
            break
        sum_b += t * hist[t]
        m_b = sum_b / w_b
        m_f = (sum_total - sum_b) / w_f
        var_between = w_b * w_f * (m_b - m_f) ** 2
        if var_between > var_max:
            var_max = var_between
            threshold = t
    return threshold

def _solve_captcha_inner(img_bytes):
    import pytesseract
    from PIL import ImageFilter, ImageEnhance, ImageOps as IOp
    try:
        from scipy.ndimage import morphology as morph
    except Exception:
        morph = None
    
    img = Image.open(BytesIO(img_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    gray = IOp.grayscale(img)
    w, h = gray.size
    big = gray.resize((w * 4, h * 4), Image.LANCZOS)
    arr = np.array(big)
    candidates = []

    def run_ocr(pil_img, tag=""):
        found = []
        for psm in [7, 8, 13, 6, 3]:
            config = f'--psm {psm} --oem 3 -c tessedit_char_whitelist=abcdefghijklmnopqrstuvwxyz'
            for attempt in range(3):
                try:
                    text = pytesseract.image_to_string(pil_img, config=config).strip()
                    text = re.sub(r'[^a-z]', '', text.lower())
                    if 3 <= len(text) <= 12:
                        found.append(text)
                    break
                except Exception as ocr_err:
                    err_str = str(ocr_err).lower()
                    if "eagain" in err_str or "resource temporarily unavailable" in err_str or "errno 11" in err_str:
                        if attempt < 2:
                            time.sleep(0.3 * (attempt + 1))
                            continue
                    break
        return found

    # 1. Otsu adaptive threshold (best for varied lighting)
    try:
        otsu_t = _otsu_threshold(arr)
        otsu_bin = Image.fromarray(((arr >= otsu_t) * 255).astype('uint8'))
        candidates.extend(run_ocr(otsu_bin, "otsu"))
    except:
        pass

    # 2. Multiple fixed thresholds (broad coverage)
    for thresh_val in range(90, 211, 20):
        binary_img = Image.fromarray(((arr >= thresh_val) * 255).astype('uint8'))
        candidates.extend(run_ocr(binary_img, f"th-{thresh_val}"))

    # 3. Inverted thresholds (for dark text on light bg)
    for thresh_val in [90, 120, 150, 180]:
        binary_img = Image.fromarray(((arr < thresh_val) * 255).astype('uint8'))
        candidates.extend(run_ocr(binary_img, f"inv-{thresh_val}"))

    # 4. Noise-cleaned with component removal
    for thresh_val in [110, 130, 150, 170]:
        binary = (arr < thresh_val).astype(np.uint8)
        cleaned = remove_small_components(binary, min_size=20)
        clean_img = Image.fromarray(((1 - cleaned) * 255).astype('uint8'))
        candidates.extend(run_ocr(clean_img, f"clean-{thresh_val}"))

    # 5. Contrast-enhanced
    try:
        enhanced = ImageEnhance.Contrast(big).enhance(3.0)
        ext_arr = np.array(enhanced)
        for thresh_val in range(100, 201, 25):
            binary_img = Image.fromarray(((ext_arr >= thresh_val) * 255).astype('uint8'))
            candidates.extend(run_ocr(binary_img, f"contr-{thresh_val}"))
    except:
        pass

    # 6. Median-blurred (removes salt-and-pepper noise)
    try:
        median = big.filter(ImageFilter.MedianFilter(size=3))
        med_arr = np.array(median)
        for thresh_val in [100, 130, 160]:
            binary_img = Image.fromarray(((med_arr >= thresh_val) * 255).astype('uint8'))
            candidates.extend(run_ocr(binary_img, f"med-{thresh_val}"))
    except:
        pass

    # 7. Morphological cleanup (closing gaps in letters)
    try:
        for thresh_val in [120, 140, 160]:
            binary = (arr < thresh_val).astype(np.uint8)
            cleaned = remove_small_components(binary, min_size=15)
            # Morphological close to join broken characters
            from scipy.ndimage import binary_closing
            closed = binary_closing(cleaned, structure=np.ones((3,3)))
            closed_img = Image.fromarray(((1 - closed) * 255).astype('uint8'))
            candidates.extend(run_ocr(closed_img, f"morph-{thresh_val}"))
    except ImportError:
        # Fallback without scipy
        try:
            for thresh_val in [120, 140, 160]:
                binary = (arr < thresh_val).astype(np.uint8)
                cleaned = remove_small_components(binary, min_size=15)
                from PIL import ImageFilter as _IF
                tmp = Image.fromarray((cleaned * 255).astype('uint8'))
                tmp = tmp.filter(_IF.MaxFilter(3)).filter(_IF.MinFilter(3))
                inv = IOp.invert(tmp)
                candidates.extend(run_ocr(inv, f"morph2-{thresh_val}"))
        except:
            pass
    except:
        pass

    # 8. Bilateral-like smoothing via blur + threshold
    try:
        blurred = big.filter(ImageFilter.GaussianBlur(radius=1))
        blr_arr = np.array(blurred)
        for thresh_val in [110, 140, 170]:
            binary_img = Image.fromarray(((blr_arr >= thresh_val) * 255).astype('uint8'))
            candidates.extend(run_ocr(binary_img, f"blur-{thresh_val}"))
    except:
        pass

    print(f"[BOT] OCR candidates ({len(candidates)}): {candidates[:30]}{'...' if len(candidates)>30 else ''}", flush=True)
    if not candidates:
        return ""

    # Use Levenshtein voting
    if WORD_LIST:
        best = _levenstein_best(candidates, WORD_LIST)
        if best:
            print(f"[BOT] OCR best: '{best}'", flush=True)
            return best

    # Fallback: most common candidate
    most_common = Counter(candidates).most_common(1)[0][0]
    print(f"[BOT] OCR fallback: '{most_common}'", flush=True)
    return most_common

def parse_wait_time(text):
    mins = re.search(r'(\d+)\s*minute', text)
    secs = re.search(r'(\d+)\s*second', text)
    total = 0
    if mins: total += int(mins.group(1)) * 60
    if secs: total += int(secs.group(1))
    return total

def resolve_comment_link(url):
    if not url:
        return None
    try:
        import urllib.request
        from urllib.parse import urlparse, parse_qs, unquote
        final_url = url
        try:
            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
            response = urllib.request.urlopen(req, timeout=15)
            final_url = response.url
            body_text = ""
            try:
                body_text = response.read(50000).decode('utf-8', errors='ignore')
            except:
                pass
        except urllib.error.HTTPError as e:
            final_url = e.headers.get('Location', url) if hasattr(e, 'headers') else url
            body_text = ""
        except Exception:
            body_text = ""
        print(f"[BOT] Comment link resolved to: {final_url}", flush=True)
        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)
        comment_id = params.get('comment', [None])[0] or params.get('reply_comment_id', [None])[0]
        if not comment_id and body_text:
            import re as _re
            comment_matches = _re.findall(r'comment=(\d+)', body_text)
            if comment_matches:
                comment_id = comment_matches[0]
            if not comment_id:
                reply_matches = _re.findall(r'reply_comment_id=(\d+)', body_text)
                if reply_matches:
                    comment_id = reply_matches[0]
            if not comment_id:
                og_matches = _re.findall(r'(?:canonical|og:url)["\']?\s*(?:content|href)=["\']([^"\']+)["\']', body_text)
                for og_url in og_matches:
                    og_parsed = urlparse(unquote(og_url))
                    og_params = parse_qs(og_parsed.query)
                    cid = og_params.get('comment', [None])[0]
                    if cid:
                        comment_id = cid
                        break
        path_parts = parsed.path.strip('/').split('/')
        video_creator = path_parts[0].lstrip('@') if path_parts else None
        video_id = None
        if 'video' in path_parts:
            idx = path_parts.index('video')
            if idx + 1 < len(path_parts):
                video_id = path_parts[idx + 1]
        print(f"[BOT] Resolved comment: comment_id={comment_id}, video_id={video_id}, creator={video_creator}", flush=True)
        return {
            'final_url': final_url,
            'comment_id': comment_id,
            'video_creator': video_creator,
            'video_id': video_id,
        }
    except Exception as e:
        print(f"[BOT] Comment link resolution failed: {e}", flush=True)
        return None

# 
#  LIVE VIDEO STREAMING
# 
class FrameBuffer:
    def __init__(self, max_frames=20):
        self.buffer = deque(maxlen=max_frames)
        self.lock = threading.Lock()
    def add_frame(self, frame_bytes):
        with self.lock:
            self.buffer.append(frame_bytes)
    def get_latest(self):
        with self.lock:
            return self.buffer[-1] if self.buffer else None
    def get_all(self):
        with self.lock:
            return list(self.buffer)

class Session:
    _counter = 0
    _lock = threading.Lock()
    def __init__(self, video_url, service="views", num_tabs=1, username=""):
        with Session._lock:
            Session._counter += 1
            self.id = Session._counter
        self.video_url = video_url
        self.service = service
        self.username = username
        self.num_tabs = max(1, min(num_tabs, 50))
        self.status = "starting"
        self.total_count = 0
        self.cycles = 0
        self.logs = []
        self.countdown = ""
        self.stop_event = threading.Event()
        self.thread = None
        self.count_lock = threading.Lock()
        self.active_tabs = 0
        self.video_buffers = {}

    @property
    def svc(self):
        return SERVICES.get(self.service, SERVICES["views"])

    def log(self, msg):
        pre = getattr(_tab_prefix, 'value', '')
        full = f"{pre}{msg}"
        self.logs.append(full)
        self.countdown = ""
        print(f"[S{self.id}] {full}", flush=True)

    def set_countdown(self, text):
        self.countdown = text

    def add_count(self, count):
        with self.count_lock:
            self.total_count += count
            return self.total_count

    def add_cycle(self):
        with self.count_lock:
            self.cycles += 1
            return self.cycles

    def to_dict(self):
        return {
            "id": self.id,
            "url": self.video_url,
            "username": self.username,
            "service": self.service,
            "serviceName": self.svc["name"],
            "serviceEmoji": self.svc["emoji"],
            "status": self.status,
            "count": self.total_count,
            "unit": self.svc["unit"],
            "cycles": self.cycles,
            "countdown": self.countdown,
            "numTabs": self.num_tabs,
            "activeTabs": self.active_tabs,
        }

sessions = {}
sessions_lock = threading.Lock()

# 
#  BOT LOOP
# 
def capture_screenshot(page, quality=60, max_width=1280):
    try:
        screenshot_bytes = page.screenshot(type='jpeg', quality=quality)
        return _resize_jpeg(screenshot_bytes, quality=quality, max_width=max_width)
    except Exception as e:
        print(f"[VIDEO] Screenshot error: {e}", flush=True)
        return None

async def capture_screenshot_async(page, quality=60, max_width=1280):
    # Async version: MUST await page.screenshot() for async Playwright pages
    try:
        screenshot_bytes = await page.screenshot(type='jpeg', quality=quality)
        return _resize_jpeg(screenshot_bytes, quality=quality, max_width=max_width)
    except Exception as e:
        print(f"[VIDEO] Async screenshot error: {e}", flush=True)
        return None

def _resize_jpeg(screenshot_bytes, quality=60, max_width=1280):
    # Resize a JPEG screenshot if wider than max_width
    try:
        img = Image.open(BytesIO(screenshot_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=quality)
            return buf.getvalue()
        return screenshot_bytes
    except Exception as e:
        print(f"[VIDEO] Resize error: {e}", flush=True)
        return screenshot_bytes

def generate_mjpeg_stream(frame_buffer, fps=2):
    frame_interval = 1.0 / fps
    last_frame_time = time.time()
    while True:
        current_time = time.time()
        if current_time - last_frame_time < frame_interval:
            time.sleep(0.01)
            continue
        frame_data = frame_buffer.get_latest()
        if frame_data is None:
            time.sleep(0.05)
            continue
        yield (b'--FRAME\r\n'
               b'Content-Type: image/jpeg\r\n'
               b'Content-Length: ' + str(len(frame_data)).encode() + b'\r\n'
               b'\r\n' + frame_data + b'\r\n')
        last_frame_time = current_time
        time.sleep(0.01)

def run_session(session):
    session.status = "running"
    svc_name = session.svc["name"]
    nt = session.num_tabs

    # Use shared browser mode when beneficial (PAGES_PER_BROWSER > 1 and multiple sessions)
    if PAGES_PER_BROWSER > 1 and nt > 1:
        import math as _math
        num_browsers = max(1, min(_math.ceil(nt / PAGES_PER_BROWSER), MAX_GLOBAL_BROWSERS))
        actual_sessions = num_browsers * PAGES_PER_BROWSER
        session.log(f" Launching {num_browsers} browsers x {PAGES_PER_BROWSER} pages = {actual_sessions} sessions (max {MAX_GLOBAL_BROWSERS} browsers) ({svc_name})...")
        import asyncio
        async def _run_all_browsers():
            tasks = []
            for bi in range(num_browsers):
                task = asyncio.create_task(_run_shared_browser_async(session, bi))
                tasks.append(task)
                await asyncio.sleep(3)
            await asyncio.gather(*tasks)
        try:
            asyncio.run(_run_all_browsers())
        except Exception as e:
            session.log(f" [BROWSER] Fatal: {e}")
            import traceback
            traceback.print_exc()
    elif nt <= 1:
        session.log(f" Launching browser ({svc_name} mode)...")
        run_tab(session, 0)
    else:
        session.log(f" Launching {nt} tabs ({svc_name} mode)...")
        threads = []
        for tab_id in range(nt):
            t = threading.Thread(target=run_tab, args=(session, tab_id), daemon=True)
            t.start()
            threads.append(t)
            time.sleep(5)
        for t in threads:
            t.join()
    if session.status == "running":
        session.log(" Session stopped.")
        session.status = "stopped"

def run_tab(session, tab_id):
    import time as _time
    if tab_id not in session.video_buffers:
        session.video_buffers[tab_id] = FrameBuffer()

    def z_sleep(seconds):
        if session.stop_event.is_set():
            raise Exception("Session stopped")
        if seconds <= 0: return
        import inspect
        frame_rec = inspect.currentframe().f_back
        pg = frame_rec.f_locals.get('page', None)
        end_t = _time.time() + seconds
        while _time.time() < end_t:
            if session.stop_event.is_set():
                raise Exception("Session stopped")
            if pg is not None:
                try:
                    frm = capture_screenshot(pg, quality=30)
                    if frm:
                        session.video_buffers[tab_id].add_frame(frm)
                except Exception:
                    pass
            remaining = end_t - _time.time()
            if remaining > 0:
                _time.sleep(min(0.5, remaining))

    # Stuck-loop detector
    session._consecutive_no_response = 0

    import gc
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc.get("menu_class", "")
    unit = svc["unit"]
    emoji = svc["emoji"]
    panel_sel = f".{menu_cls}" if menu_cls else ""
    input_panel_sel = f".{menu_cls} input[placeholder='Enter Video URL']:visible" if menu_cls else 'input[placeholder="Enter Video URL"]:visible, input[type="search"]:visible'
    submit_panel_sel = f".{menu_cls} button[type='submit']" if menu_cls else 'button:has-text("Search"):visible'
    results_panel_sel = f".{menu_cls} div[id]" if menu_cls else ""
    multi = session.num_tabs > 1
    _tab_prefix.value = f"[T{tab_id+1}] " if multi else ""

    MAX_FULL_RESTARTS = 100
    backoff = 5
    with session.count_lock:
        session.active_tabs += 1
    try:
        for full_restart in range(MAX_FULL_RESTARTS):
            if session.stop_event.is_set():
                return
            if full_restart > 0:
                wait_time = min(int(backoff), 30)
                session.log(f" Full restart #{full_restart} (waiting {wait_time}s)...")
                z_sleep(wait_time)
                backoff = min(backoff * 1.5, 30)
                gc.collect()
                if USING_TOR:
                    session.log(" Requesting fresh Tor IP...")
                    renew_tor_circuit()
            else:
                if multi:
                    session.log(f" Starting tab...")

            # Reset stuck-loop counter on restart
            session._consecutive_no_response = 0

            browser = None
            page = None
            got_slot = False
            try:
                if not _browser_semaphore.acquire(timeout=1):
                    session.log(f" Waiting for browser slot (max {MAX_GLOBAL_BROWSERS} globally)...")
                    _browser_semaphore.acquire()
                got_slot = True
                with _active_browsers_lock:
                    global _active_browsers
                    _active_browsers += 1
                    session.log(f" Browser slot acquired ({_active_browsers}/{MAX_GLOBAL_BROWSERS} in use)")
            except Exception:
                pass

            try:
                with sync_playwright() as p:
                    launch_opts = {
                        "headless": True,
                        "args": [
                            "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                            "--disable-extensions", "--disable-background-networking",
                            "--disable-default-apps", "--disable-sync", "--disable-translate",
                            "--no-first-run", "--disable-background-timer-throttling",
                            "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                            "--disable-component-extensions-with-background-pages",
                            "--disable-features=TranslateUI", "--renderer-process-limit=1",
                            "--js-flags=--max-old-space-size=128", "--disable-software-rasterizer",
                            "--disable-logging", "--disable-hang-monitor",
                            "--disable-ipc-flooding-protection", "--memory-pressure-off",
                        ],
                    }
                    if USING_TOR:
                        tor_port = 9050 + (tab_id % 10)
                        if full_restart == 0:
                            session.log(f" Routing through Tor (port {tor_port})...")
                        for _tw in range(60):
                            if os.path.exists("/tmp/tor_ready"):
                                break
                            if _tw == 0:
                                session.log(" Waiting for Tor to bootstrap...")
                            z_sleep(1)
                        launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
                    elif PROXY_URL:
                        if full_restart == 0:
                            session.log(f" Using proxy: {PROXY_URL.split('@')[-1] if '@' in PROXY_URL else PROXY_URL}")
                        launch_opts["proxy"] = {"server": PROXY_URL}

                    browser = p.chromium.launch(slow_mo=100, **launch_opts)
                    page = browser.new_page(viewport={"width": 800, "height": 600})
                    page.on("dialog", lambda d: d.accept())

                    def safe_check(pg):
                        try:
                            pg.title()
                            return True
                        except:
                            return False

                    session.log(" Loading zefoy.com...")
                    try:
                        page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                    except Exception as _goto_err:
                        session.log(f" Page crashed on load ({_goto_err}), restarting...")
                        continue

                    z_sleep(5)
                    inject_anti_detection(page)
                    if not safe_check(page):
                        session.log(" Page crashed on load, restarting...")
                        continue

                    session.log(" Checking for captcha...")
                    captcha_detected = False
                    page_ready = False

                    for page_attempt in range(10):
                        if session.stop_event.is_set():
                            return
                        if not safe_check(page):
                            session.log(" Crashed during page check, restarting...")
                            break
                        try:
                            page_title = page.title().lower()
                            page_text = page.inner_text("body")[:200].lower()
                            if "502" in page_title or "502 bad gateway" in page_text:
                                session.log(f" Zefoy is down (502 error), retrying ({page_attempt + 1}/10)...")
                                z_sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                z_sleep(5)
                                continue
                            if "503" in page_title or "cloudflare" in page_text or "just a moment" in page_text:
                                session.log(f" Zefoy loading/Cloudflare check ({page_attempt + 1}/10)...")
                                z_sleep(10 + page_attempt * 3)
                                page.reload(wait_until="domcontentloaded")
                                z_sleep(5)
                                continue
                        except:
                            pass

                        try:
                            page.locator("#captcha-img, .wrapper-capth, input[name='captchalogin'], img[src*='captcha'], img[src*='CAPTCHA']").first.wait_for(state="visible", timeout=30000)
                            captcha_detected = True
                            break
                        except:
                            pass

                        try:
                            page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=20000)
                            session.log(" No captcha needed  service buttons already visible")
                            page_ready = True
                            break
                        except:
                            pass

                        session.log(f"  Page not ready, reloading (attempt {page_attempt + 1}/10)...")
                        try:
                            page.reload(wait_until="domcontentloaded")
                        except Exception as _reload_err:
                            session.log(f"  Error: {_reload_err}  restarting tab...")
                            break
                        z_sleep(10 + page_attempt * 3)
                    else:
                        session.log("  Page never became ready, restarting...")
                        continue

                    if not captcha_detected and not page_ready:
                        continue

                    if captcha_detected:
                        session.log(f" Waiting for a captcha-solving slot (max {CAPTCHA_CONCURRENCY} concurrent)...")
                        try:
                            _captcha_semaphore.acquire()
                            session.log(" Acquired captcha-solving slot, starting...")
                            try:
                                captcha_solved = False
                                for captcha_attempt in range(8):
                                    if session.stop_event.is_set():
                                        return
                                    if not safe_check(page):
                                        session.log(" Crashed during captcha, restarting...")
                                        break
                                    try:
                                        captcha_img = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                                        try:
                                            captcha_img.first.wait_for(state="visible", timeout=10000)
                                        except:
                                            session.log("  Captcha image not loading, reloading page...")
                                            page.reload(wait_until="domcontentloaded")
                                            z_sleep(5)
                                            continue

                                        session.log(f" Solving captcha ({captcha_attempt + 1}/8)...")
                                        z_sleep(1)
                                        captcha_bytes = captcha_img.first.screenshot()
                                        answer = solve_captcha(captcha_bytes)

                                        if not answer:
                                            session.log("  OCR failed (bad image or unsupported chars), refreshing captcha...")
                                            try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                            except: page.reload(wait_until="domcontentloaded")
                                            z_sleep(3)
                                            continue

                                        session.log(f" OCR answer: '{answer}'")
                                        remove_overlays(page)
                                        z_sleep(0.5)

                                        # Real zefoy captcha input selectors (matches current DOM)
                                        captcha_input = page.locator(
                                            "input[name='captchalogin'],"
                                            " input.captcha-login-input,"
                                            " input[placeholder='Enter the word']"
                                        )
                                        try:
                                            captcha_input.first.wait_for(state="visible", timeout=8000)
                                        except:
                                            try:
                                                captcha_input.first.wait_for(state="attached", timeout=5000)
                                            except:
                                                session.log("  Captcha input not in DOM, refreshing...")
                                                try:
                                                    page.locator(".refresh-capthca-btn-new").first.click()
                                                except:
                                                    page.reload(wait_until="domcontentloaded")
                                                z_sleep(3)
                                                continue

                                        try:
                                            captcha_input.first.fill(answer)
                                        except:
                                            session.log("  fill() failed, typing via keyboard...")
                                            try:
                                                captcha_input.first.click()
                                                z_sleep(0.2)
                                                page.keyboard.type(answer, delay=80)
                                            except Exception as kb_err:
                                                session.log(f"  Keyboard fallback also failed: {kb_err}")
                                                try:
                                                    page.locator(".refresh-capthca-btn-new").first.click()
                                                except:
                                                    page.reload(wait_until="domcontentloaded")
                                                z_sleep(3)
                                                continue

                                        z_sleep(0.5)
                                        remove_overlays(page)
                                        z_sleep(0.3)

                                        page.locator("button.submit-captcha").first.click()
                                        z_sleep(5)

                                        try:
                                            page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                            session.log(" Captcha solved!")
                                            inject_anti_detection(page)
                                            captcha_solved = True
                                            break
                                        except:
                                            session.log(f" Wrong answer '{answer}', retrying...")
                                            # Real zefoy error modal is #zbcd
                                            try:
                                                page.locator("#zbcd .btn-secondary, #zbcd button[data-dismiss='modal'], .modal.show .btn-secondary").first.click()
                                            except: pass
                                            z_sleep(1)
                                            try: page.locator(".refresh-capthca-btn-new, [onclick*='refresh'], .captcha-refresh").first.click()
                                            except: pass
                                            z_sleep(3)

                                    except Exception as e:
                                        if is_dead(e):
                                            session.log(" Crashed during captcha, restarting...")
                                            break
                                        else:
                                            session.log(f"  Captcha error: {e}")
                                        z_sleep(2)

                                if not captcha_solved:
                                    session.log(" All captcha attempts failed (sync mode), restarting browser...")
                                    continue
                            finally:
                                _captcha_semaphore.release()
                                session.log(" Released captcha-solving slot")
                        except Exception as e:
                            session.log(f" Captcha semaphore error: {e}")
                            continue

                    #  Click service button 
                    session.log(f"{emoji} Looking for {svc_name} button...")
                    try:
                        page.locator(f".{btn_cls}").wait_for(timeout=30000)
                    except:
                        try:
                            btn_el = page.locator(f".{btn_cls}")
                            if btn_el.count() > 0 and btn_el.get_attribute("disabled"):
                                session.log(f" {svc_name} is currently unavailable on Zefoy. Try a different service.")
                            else:
                                session.log(f" {svc_name} button not found. Restarting...")
                        except:
                            session.log(f" {svc_name} button not found. Restarting...")
                        continue

                    try:
                        btn_element = page.locator(f".{btn_cls}").first
                        btn_element.scroll_into_view_if_needed()
                        z_sleep(0.5)
                        remove_overlays(page)
                        z_sleep(0.3)
                        try:
                            btn_element.click(timeout=5000)
                        except:
                            remove_overlays(page)
                            z_sleep(0.3)
                            btn_element.click(force=True, timeout=10000)
                    except Exception as btn_err:
                        if is_dead(btn_err):
                            session.log(f" Crashed clicking {svc_name} button, restarting...")
                            continue
                        session.log(f"  Error clicking button: {btn_err}, restarting...")
                        continue

                    z_sleep(2)
                    inject_anti_detection(page)
                    session.log(f" {svc_name} panel opened!")
                    backoff = 5
                    url_filled = False
                    input_fail_count = 0
                    MAX_INPUT_FAILS = 5

                    #  Main loop 
                    while not session.stop_event.is_set():
                        if not safe_check(page):
                            session.log(" Page crashed in main loop, restarting...")
                            break
                        cycle = session.add_cycle()
                        session.log(f" Cycle {cycle}")
                        try:
                            url_input = page.locator(input_panel_sel).first
                            try:
                                url_input.wait_for(state="visible", timeout=5000)
                                input_fail_count = 0
                            except:
                                session.log(f"  Input not visible, re-opening {svc_name} panel...")
                                try:
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(f".{btn_cls}").first.click(force=True)
                                    z_sleep(2)
                                    url_input.wait_for(state="visible", timeout=10000)
                                    input_fail_count = 0
                                except:
                                    input_fail_count += 1
                                    try:
                                        body_snip = page.inner_text("body")[:300].lower()
                                    except:
                                        session.log(" Page unreadable, restarting browser...")
                                        break
                                    if "502" in body_snip or "bad gateway" in body_snip or "503" in body_snip:
                                        session.log(" Zefoy is down (502/503), restarting browser...")
                                        break
                                    if page.locator("#captcha-img, img[src*='captcha'], img[src*='CAPTCHA']").count() > 0:
                                        session.log(" Session expired (captcha shown again), restarting browser...")
                                        break
                                    if input_fail_count >= MAX_INPUT_FAILS:
                                        session.log(f" Input not found after {MAX_INPUT_FAILS} attempts, restarting browser...")
                                        break
                                    session.log(f"  Still can't find input after re-open, retrying ({input_fail_count}/{MAX_INPUT_FAILS})...")
                                    z_sleep(3)
                                    continue
                                url_filled = False

                            if not url_filled:
                                url_input.fill("")
                                z_sleep(0.3)
                                url_input.fill(session.video_url)
                                z_sleep(1)
                                url_filled = True
                                session.log(f" URL filled")

                            submit_sel = submit_panel_sel
                            remove_overlays(page)
                            z_sleep(0.3)
                            page.locator(submit_sel).first.click()
                            z_sleep(3)

                        except Exception as fill_err:
                            if is_dead(fill_err):
                                session.log(" Crashed filling URL, restarting...")
                                break
                            session.log(f"  Error: {fill_err}")
                            z_sleep(3)
                            continue

                        #  Comment Hearts 
                        if session.service == "comment_hearts":
                            target_user = session.username.lstrip('@').lower()
                            try:
                                body_check = page.inner_text("body").lower()
                            except:
                                session.log(" Page crash, restarting...")
                                break
                            if "too many" in body_check or "slow down" in body_check:
                                session.log("  Too many requests, clicking Search...")
                                try:
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                z_sleep(3)
                                continue
                            if "please wait" in body_check and ("minute" in body_check or "second" in body_check):
                                wait_secs = parse_wait_time(body_check)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f" Countdown: {wait_secs}s")
                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f" {time_str}")
                                    z_sleep(1)
                                session.set_countdown("")
                                session.log(" Countdown done  clicking Search...")
                                try:
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                z_sleep(3)
                                continue
                            if page.locator(".kadi-rengi").count() > 0:
                                session.log(" Comments already visible")
                            else:
                                try:
                                    count_btn = page.locator(f"{HEARTS_BTN_SEL}:visible, button.wbutton:visible").first
                                    count_btn.wait_for(state="visible", timeout=20000)
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    count_btn.click()
                                    z_sleep(4)
                                    session.log(" Comments loaded")
                                except:
                                    try:
                                        snippet = page.inner_text("body")[:150]
                                        session.log(f"   button not found. Panel: {snippet}")
                                    except:
                                        session.log("   button not found, panel unreadable")
                                    try:
                                        remove_overlays(page)
                                        z_sleep(0.3)
                                        page.locator(submit_panel_sel).first.click()
                                    except:
                                        pass
                                    z_sleep(3)
                                    continue

                            found_user = False
                            crashed = False
                            max_pages = 250
                            for pg in range(max_pages):
                                if session.stop_event.is_set():
                                    break
                                try:
                                    result = page.evaluate("""(targetUser) => {
                                        const forms = document.querySelectorAll('form.w1a');
                                        const users = [];
                                        for (let i = 0; i < forms.length; i++) {
                                            const userEl = forms[i].querySelector('.kadi-rengi');
                                            if (!userEl) continue;
                                            const uname = userEl.innerText.trim().replace('@','').toLowerCase();
                                            users.push(uname);
                                            if (uname === targetUser) {
                                                return {found: true, index: i, total: forms.length};
                                            }
                                        }
                                        const nextBtn = document.querySelector('li[title="Next"] button');
                                        const hasNext = nextBtn && !nextBtn.disabled;
                                        return {found: false, total: forms.length, users: users, hasNext: hasNext};
                                    }""", target_user)
                                    if result.get('found'):
                                        idx = result['index']
                                        form_loc = page.locator("form.w1a").nth(idx)
                                        form_loc.locator("select[name='select_lmt']").select_option("100")
                                        z_sleep(1)
                                        remove_overlays(page)
                                        z_sleep(0.3)
                                        form_loc.locator("button[type='submit']").click()
                                        session.log(f" Sent 100 hearts to @{target_user} (page {pg + 1})")
                                        found_user = True
                                        z_sleep(3)
                                        break
                                    if result.get('hasNext'):
                                        if pg == 0:
                                            session.log(f" @{target_user} not on page 1, paginating...")
                                        page.locator('li[title="Next"] button').click()
                                        try:
                                            page.locator("form.w1a").first.wait_for(state="visible", timeout=3000)
                                        except:
                                            pass
                                        z_sleep(1.5)
                                    else:
                                        total_scanned = (pg * 40) + result.get('total', 0)
                                        session.log(f" @{target_user} not found in {total_scanned} comments ({pg + 1} pages)")
                                        break
                                except Exception as ce:
                                    if is_dead(ce):
                                        crashed = True
                                        session.log(" Crashed during pagination, restarting...")
                                        break
                                    session.log(f"  Pagination error: {ce}")
                                    break

                            if crashed:
                                break
                            if not found_user:
                                z_sleep(2)
                                try:
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_panel_sel).first.click()
                                except:
                                    pass
                                z_sleep(3)
                                continue

                            try:
                                body = page.inner_text("body").lower()
                            except:
                                break
                            if "successfully" in body:
                                session.add_count(100)
                                session.log(f" +100 hearts to @{target_user} (total: {session.total_count})")
                            elif "too many" in body or "slow down" in body:
                                session.log("  Too many requests")
                            z_sleep(2)
                            try:
                                remove_overlays(page)
                                z_sleep(0.3)
                                page.locator(submit_panel_sel).first.click()
                            except:
                                pass
                            z_sleep(3)
                            continue

                        #  Response handler with stuck-loop detection 
                        max_checks = 60
                        crashed = False
                        cycle_succeeded = False

                        for check_i in range(max_checks):
                            if session.stop_event.is_set():
                                break
                            try:
                                body = page.inner_text("body")
                            except Exception as e:
                                if is_dead(e):
                                    crashed = True
                                    break
                                z_sleep(1)
                                continue

                            lower_body = body.lower()

                            # Too many requests
                            if "too many" in lower_body or "slow down" in lower_body:
                                session.log("  Too many requests  clicking Search again...")
                                z_sleep(2)
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    z_sleep(3)
                                except:
                                    pass
                                continue

                            # Countdown / rate limit
                            if ("please wait" in lower_body and ("minute" in lower_body or "second" in lower_body)):
                                wait_secs = parse_wait_time(body)
                                if wait_secs <= 0:
                                    wait_secs = 60
                                wait_secs += 3
                                session.log(f" Countdown: {wait_secs}s")
                                for remaining in range(wait_secs, 0, -1):
                                    if session.stop_event.is_set():
                                        break
                                    mins = remaining // 60
                                    secs = remaining % 60
                                    time_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"
                                    session.set_countdown(f" {time_str}")
                                    z_sleep(1)
                                session.set_countdown("")
                                session.log(" Countdown done  clicking Search 2x...")
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    z_sleep(1)
                                    page.locator(submit_sel).first.click()
                                    z_sleep(3)
                                except:
                                    pass
                                continue

                            # Ready
                            if "ready" in lower_body and "next submit" in lower_body:
                                session.log(" Ready  clicking Search...")
                                try:
                                    submit_sel = submit_panel_sel
                                    remove_overlays(page)
                                    z_sleep(0.3)
                                    page.locator(submit_sel).first.click()
                                    z_sleep(3)
                                except:
                                    pass
                                continue

                            # Success
                            if "successfully" in lower_body:
                                count = 0
                                for line in body.split('\n'):
                                    if 'successfully' in line.lower():
                                        session.log(f" Raw: {line.strip()[:120]}")
                                        try:
                                            nums = [int(m) for m in re.findall(r'\d+', line) if not (2020 <= int(m) <= 2035) and int(m) < 100000]
                                        except:
                                            nums = []
                                        if nums:
                                            count = max(nums)
                                        break
                                new_total = session.add_count(count)
                                if count > 0:
                                    session.log(f" +{count} {unit}! Total: {new_total:,}")
                                else:
                                    session.log(f" Success (count not captured). Total: {new_total:,}")
                                session._consecutive_no_response = 0
                                cycle_succeeded = True
                                break

                            # Send button visible
                            send_clicked = False
                            try:
                                remove_overlays(page)
                                z_sleep(0.3)
                                _send_selectors = []
                                if panel_sel:
                                    _send_selectors += [f'{panel_sel} {HEARTS_BTN_SEL}:visible', f'{panel_sel} button.btn-dark:visible', f'{panel_sel} button.wbutton:visible', f'{panel_sel} button.btn-success:visible']
                                _send_selectors += [f'{HEARTS_BTN_SEL}:visible', 'button.btn-dark:visible', 'button.wbutton:visible', 'button.btn-success:visible']
                                for send_sel in _send_selectors:
                                    try:
                                        send_btn = page.locator(send_sel).first
                                        if send_btn.is_visible(timeout=2000):
                                            send_btn.click()
                                            send_clicked = True
                                            session.log(f"{emoji} Clicked send button!")
                                            z_sleep(3)
                                            break
                                    except:
                                        continue
                                if not send_clicked:
                                    send_clicked = page.evaluate("""() => {
                                        const btns = document.querySelectorAll('button');
                                        for (const b of btns) {
                                            const cls = b.className || '';
                                            const rect = b.getBoundingClientRect();
                                            if (cls.includes('btn-dark') && rect.width > 0 && rect.height > 0) {
                                                b.click(); return true;
                                            }
                                        }
                                        for (const b of btns) {
                                            const cls = b.className || '';
                                            const rect = b.getBoundingClientRect();
                                            if (cls.includes('wbutton') && rect.width > 0 && rect.height > 0) {
                                                b.click(); return true;
                                            }
                                        }
                                        return false;
                                    }""")
                                    if send_clicked:
                                        session.log(f"{emoji} Clicked send button (JS fallback)!")
                                        z_sleep(3)
                                if send_clicked:
                                    session._consecutive_no_response = 0
                                    cycle_succeeded = True
                                    continue
                            except:
                                pass

                            # Still loading / waiting
                            if check_i < 30:
                                z_sleep(1)
                                continue
                            else:
                                #  Stuck-loop detection 
                                session._consecutive_no_response += 1
                                streak = session._consecutive_no_response

                                if streak >= 3:
                                    # Dump page state for debugging
                                    try:
                                        page_snippet = page.inner_text("body")[:400]
                                        session.log(f" Page stuck (streak {streak}). Body: {page_snippet}")
                                    except:
                                        session.log(f" Page stuck (streak {streak}). Cannot read body.")

                                    # Check if captcha reappeared
                                    try:
                                        if page.locator("#captcha-img, img[src*='captcha']").count() > 0:
                                            session.log(" Captcha reappeared, restarting browser...")
                                            break
                                    except:
                                        pass

                                    # Check for error state
                                    if "error" in lower_body or "invalid" in lower_body:
                                        session.log(" Page showing error, restarting browser...")
                                        break

                                    # Force restart after 5 consecutive no-response
                                    if streak >= 5:
                                        session.log(f" Stuck for {streak} cycles, forcing browser restart...")
                                        break

                                session.log(f"  No response after {check_i}s (streak: {streak}), retrying...")
                                break

                        if crashed:
                            session.log(" Crashed in main loop, restarting tab...")
                            break

                        z_sleep(2)
                        if cycle % 10 == 0:
                            gc.collect()

            except Exception as inner_err:
                if is_dead(inner_err):
                    session.log(f" Browser crashed, restarting tab...")
                else:
                    import traceback
                    session.log(f"  Error: {inner_err}  restarting tab...")
                    traceback.print_exc()
            finally:
                try:
                    if browser:
                        browser.close()
                except:
                    pass
                if got_slot:
                    with _active_browsers_lock:
                        _active_browsers = max(0, _active_browsers - 1)
                    _browser_semaphore.release()
                    got_slot = False
                gc.collect()

        session.log(" Tab exhausted all restart attempts.")
    except Exception as e:
        session.log(f" Fatal error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


# 
#  SHARED BROWSER MODE — async Playwright (thread-safe, concurrent pages)
# 
async def _run_page_async(session, browser, tab_id, browser_idx):
    """Run one Zefoy session using a shared browser page (async)."""
    import gc as _gc
    svc = session.svc
    svc_name = svc["name"]
    btn_cls = svc["button_class"]
    menu_cls = svc.get("menu_class", "")
    unit = svc["unit"]
    emoji = svc["emoji"]
    panel_sel = f".{menu_cls}" if menu_cls else ""
    input_panel_sel = f".{menu_cls} input[placeholder='Enter Video URL']:visible" if menu_cls else 'input[placeholder="Enter Video URL"]:visible, input[type="search"]:visible'
    submit_panel_sel = f".{menu_cls} button[type='submit']" if menu_cls else 'button:has-text("Search"):visible'
    multi = session.num_tabs > 1
    _tab_prefix.value = f"[P{tab_id+1}] " if multi else ""

    async def z_sleep(seconds):
        if session.stop_event.is_set():
            raise Exception("Session stopped")
        if seconds <= 0:
            return
        end_t = time.time() + seconds
        while time.time() < end_t:
            if session.stop_event.is_set():
                raise Exception("Session stopped")
            await asyncio.sleep(min(0.5, end_t - time.time()))

    with session.count_lock:
        session.active_tabs += 1
    if tab_id not in session.video_buffers:
        session.video_buffers[tab_id] = FrameBuffer()
    try:
        page = None
        captcha_cycles = 0  # track total captcha retry cycles
        for restart in range(50):
            if session.stop_event.is_set():
                return
            if restart > 0:
                await z_sleep(5)
            
            # Hard cap: if captcha keeps failing after 4 full page-reload cycles, pause
            if captcha_cycles >= 4:
                session.log(" Captcha failed after 4 full retry cycles, pausing 60s...")
                await z_sleep(60)
                captcha_cycles = 0

            # Create a FRESH page from the shared browser on each restart
            try:
                if page:
                    try:
                        await page.close()
                    except:
                        pass
                page = await browser.new_page(viewport={"width": 800, "height": 600})
                page.on("dialog", lambda d: d.accept())
            except Exception as pe:
                session.log(f" Page creation error: {pe}")
                continue

            page_closed = False

            # Continuous live-cam capture task: screenshots ~2fps while page is alive
            cam_task = None
            async def _cam_loop():
                try:
                    while not session.stop_event.is_set():
                        try:
                            if page is not None and not page.is_closed():
                                frm = await capture_screenshot_async(page, quality=30)
                                if frm and tab_id in session.video_buffers:
                                    session.video_buffers[tab_id].add_frame(frm)
                        except Exception:
                            pass
                        await asyncio.sleep(0.5)
                except Exception:
                    pass

            try:
                cam_task = asyncio.ensure_future(_cam_loop())
            except Exception:
                cam_task = None

            try:
                # Navigate to zefoy
                session.log(" Loading zefoy.com...")
                try:
                    await page.goto(ZEFOY, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    session.log(f" Load error: {e}")
                    page_closed = True
                    continue

                await z_sleep(5)
                await inject_anti_detection_async(page)

                # Check page is alive
                async def safe_check():
                    try:
                        await page.title()
                        return True
                    except:
                        return False
                if not await safe_check():
                    continue

                # ---- Captcha handling ----
                session.log(" Checking for captcha...")
                captcha_detected = False
                page_ready = False
                for pa in range(10):
                    if session.stop_event.is_set():
                        return
                    if not await safe_check():
                        break
                    try:
                        t = (await page.title()).lower()
                        b = (await page.inner_text("body"))[:200].lower()
                        if "502" in t or "502 bad gateway" in b:
                            await z_sleep(10 + pa * 3)
                            await page.reload(wait_until="domcontentloaded")
                            await z_sleep(5)
                            continue
                        if "503" in t or "cloudflare" in b or "just a moment" in b:
                            await z_sleep(10 + pa * 3)
                            await page.reload(wait_until="domcontentloaded")
                            await z_sleep(5)
                            continue
                    except:
                        pass
                    try:
                        loc = "#captcha-img, .wrapper-capth, input[name='captchalogin'], img[src*='captcha']"
                        await page.locator(loc).first.wait_for(state="visible", timeout=30000)
                        captcha_detected = True
                        break
                    except:
                        pass
                    try:
                        await page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=20000)
                        page_ready = True
                        break
                    except:
                        pass
                    session.log(f" Page not ready ({pa+1}/10)...")
                    try:
                        await page.reload(wait_until="domcontentloaded")
                    except:
                        break
                    await z_sleep(10 + pa * 3)
                else:
                    continue
                if not captcha_detected and not page_ready:
                    continue

                if captcha_detected:
                    session.log(f" Captcha slot (max {CAPTCHA_CONCURRENCY})...")
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, _captcha_semaphore.acquire)
                    try:
                        captcha_solved = False
                        for ca in range(8):
                            if session.stop_event.is_set():
                                return
                            if not await safe_check():
                                break
                            try:
                                ci = page.locator("#captcha-img, img[src*='CAPTCHA'], img[src*='captcha']")
                                try:
                                    await ci.first.wait_for(state="visible", timeout=10000)
                                except:
                                    await page.reload(wait_until="domcontentloaded")
                                    await z_sleep(5)
                                    continue
                                session.log(f" Solving captcha ({ca+1}/8)...")
                                await z_sleep(2)
                                # OCR is CPU/subprocess-bound: run in executor so it doesn't
                                # freeze the shared event loop for all pages.
                                answer = await loop.run_in_executor(None, solve_captcha, await ci.first.screenshot())
                                if not answer:
                                    session.log("  OCR returned empty, refreshing captcha...")
                                    try:
                                        await page.locator(".refresh-capthca-btn-new, [onclick*='refresh']").first.click()
                                    except:
                                        await page.reload(wait_until="domcontentloaded")
                                    await z_sleep(3)
                                    continue
                                session.log(f" OCR answer: '{answer}'")
                                await remove_overlays_async(page)
                                await z_sleep(0.5)
                                inp = page.locator("input[name='captchalogin'], input.captcha-login-input, input[placeholder='Enter the word']")
                                try:
                                    await inp.first.wait_for(state="visible", timeout=8000)
                                except:
                                    try:
                                        await inp.first.wait_for(state="attached", timeout=5000)
                                    except:
                                        try:
                                            await page.locator(".refresh-capthca-btn-new").first.click()
                                        except:
                                            await page.reload(wait_until="domcontentloaded")
                                        await z_sleep(3)
                                        continue
                                try:
                                    await inp.first.fill(answer)
                                except:
                                    try:
                                        await inp.first.click()
                                        await z_sleep(0.2)
                                        await page.keyboard.type(answer, delay=80)
                                    except:
                                        try:
                                            await page.locator(".refresh-capthca-btn-new").first.click()
                                        except:
                                            await page.reload(wait_until="domcontentloaded")
                                        await z_sleep(3)
                                        continue
                                await z_sleep(0.5)
                                await remove_overlays_async(page)
                                await z_sleep(0.3)
                                await page.locator("button.submit-captcha").first.click()
                                await z_sleep(5)
                                try:
                                    await page.locator(ANY_SERVICE_BUTTON).first.wait_for(timeout=8000)
                                    session.log(" Captcha solved!")
                                    await inject_anti_detection_async(page)
                                    captcha_solved = True
                                    break
                                except:
                                    session.log(f" Wrong answer '{answer}'...")
                                    try:
                                        await page.locator("#zbcd .btn-secondary, #zbcd button[data-dismiss='modal'], .modal.show .btn-secondary").first.click()
                                    except:
                                        pass
                                    await z_sleep(1)
                                    try:
                                        await page.locator(".refresh-capthca-btn-new, [onclick*='refresh']").first.click()
                                    except:
                                        pass
                                    await z_sleep(3)
                            except Exception as e:
                                if is_dead(e):
                                    break
                                session.log(f"  Captcha error: {e}")
                                await z_sleep(2)
                        if not captcha_solved:
                            session.log(" All 8 captcha attempts failed, will retry on next page load...")
                            captcha_cycles += 1
                            continue
                    finally:
                        _captcha_semaphore.release()

                # ---- Click service button ----
                session.log(f"{emoji} Opening {svc_name} panel...")
                try:
                    await page.locator(f".{btn_cls}").wait_for(timeout=30000)
                except:
                    session.log(f" {svc_name} button not found.")
                    continue
                try:
                    be = page.locator(f".{btn_cls}").first
                    await be.scroll_into_view_if_needed()
                    await z_sleep(0.5)
                    await remove_overlays_async(page)
                    await z_sleep(0.3)
                    try:
                        await be.click(timeout=5000)
                    except:
                        await remove_overlays_async(page)
                        await z_sleep(0.3)
                        await be.click(force=True, timeout=10000)
                except Exception as e:
                    if not is_dead(e):
                        session.log(f" Click: {e}")
                    continue
                await z_sleep(2)
                await inject_anti_detection_async(page)
                session.log(f" {svc_name} panel opened!")

                url_filled = False
                input_fail_count = 0

                # ---- Main Zefoy loop ----
                while not session.stop_event.is_set():
                    if not await safe_check():
                        session.log(" Page crashed, restarting...")
                        break
                    cycle = session.add_cycle()
                    try:
                        url_input = page.locator(input_panel_sel).first
                        try:
                            await url_input.wait_for(state="visible", timeout=5000)
                            input_fail_count = 0
                        except:
                            try:
                                await remove_overlays_async(page)
                                await z_sleep(0.3)
                                await page.locator(f".{btn_cls}").first.click(force=True)
                                await z_sleep(2)
                                await url_input.wait_for(state="visible", timeout=10000)
                                input_fail_count = 0
                            except:
                                input_fail_count += 1
                                try:
                                    bs = (await page.inner_text("body"))[:300].lower()
                                except:
                                    break
                                if "502" in bs or "bad gateway" in bs or "503" in bs:
                                    break
                                if await page.locator("#captcha-img, img[src*='captcha']").count() > 0:
                                    break
                                if input_fail_count >= 5:
                                    break
                                await z_sleep(3)
                                continue
                            url_filled = False
                        if not url_filled:
                            await url_input.fill("")
                            await z_sleep(0.3)
                            await url_input.fill(session.video_url)
                            await z_sleep(1)
                            url_filled = True
                        await remove_overlays_async(page)
                        await z_sleep(0.3)
                        await page.locator(submit_panel_sel).first.click()
                        await z_sleep(3)
                    except Exception as e:
                        if is_dead(e):
                            break
                        await z_sleep(3)
                        continue

                    # ---- Comment Hearts fast search ----
                    if session.service == "comment_hearts":
                        target_user = session.username.lstrip('@').lower()
                        try:
                            body_check = (await page.inner_text("body")).lower()
                        except:
                            break
                        if "too many" in body_check or "slow down" in body_check:
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        if "please wait" in body_check and ("minute" in body_check or "second" in body_check):
                            wt = parse_wait_time(body_check)
                            if wt <= 0: wt = 60
                            wt += 3
                            session.log(f" Countdown: {wt}s")
                            for r in range(wt, 0, -1):
                                if session.stop_event.is_set():
                                    break
                                m, s = r // 60, r % 60
                                session.set_countdown(f" {m}m {s:02d}s" if m else f" {s}s")
                                await z_sleep(1)
                            session.set_countdown("")
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        if await page.locator(".kadi-rengi").count() == 0:
                            try:
                                cb = page.locator(f"{HEARTS_BTN_SEL}:visible, button.wbutton:visible").first
                                await cb.wait_for(state="visible", timeout=20000)
                                await remove_overlays_async(page)
                                await z_sleep(0.3)
                                await cb.click()
                                await z_sleep(4)
                                session.log(" Comments loaded")
                            except:
                                await remove_overlays_async(page)
                                await z_sleep(0.3)
                                await page.locator(submit_panel_sel).first.click()
                                await z_sleep(3)
                                continue

                        found_user = False
                        for pg in range(250):
                            if session.stop_event.is_set():
                                break
                            try:
                                result = await page.evaluate("""(targetUser) => {
                                    const forms = document.querySelectorAll('form.w1a');
                                    for (let i = 0; i < forms.length; i++) {
                                        const el = forms[i].querySelector('.kadi-rengi');
                                        if (!el) continue;
                                        if (el.innerText.trim().replace('@','').toLowerCase() === targetUser) {
                                            return {found: true, index: i};
                                        }
                                    }
                                    const nb = document.querySelector('li[title="Next"] button');
                                    return {found: false, hasNext: !!(nb && !nb.disabled)};
                                }""", target_user)
                                if result.get('found'):
                                    form_loc = page.locator("form.w1a").nth(result['index'])
                                    await form_loc.locator("select[name='select_lmt']").select_option("100")
                                    await z_sleep(0.5)
                                    await remove_overlays_async(page)
                                    await z_sleep(0.3)
                                    await form_loc.locator("button[type='submit']").click()
                                    session.log(f" Sent 100 hearts to @{target_user}")
                                    found_user = True
                                    await z_sleep(3)
                                    break
                                if result.get('hasNext'):
                                    if pg == 0:
                                        session.log(f" Searching @{target_user}...")
                                    await page.locator('li[title="Next"] button').click()
                                    try:
                                        await page.locator("form.w1a").first.wait_for(state="visible", timeout=3000)
                                    except:
                                        pass
                                    await z_sleep(1.5)
                                else:
                                    session.log(f" @{target_user} not found")
                                    break
                            except Exception as e:
                                if is_dead(e):
                                    break
                                session.log(f" Search: {e}")
                                break
                        if not found_user:
                            await z_sleep(2)
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        try:
                            body = (await page.inner_text("body")).lower()
                        except:
                            break
                        if "successfully" in body:
                            session.add_count(100)
                            session.log(f" +100 hearts to @{target_user} (total: {session.total_count})")
                        elif "too many" in body or "slow down" in body:
                            session.log(" Too many requests")
                        await z_sleep(2)
                        await remove_overlays_async(page)
                        await z_sleep(0.3)
                        await page.locator(submit_panel_sel).first.click()
                        await z_sleep(3)
                        continue

                    # ---- Regular response handler ----
                    for check_i in range(60):
                        if session.stop_event.is_set():
                            break
                        try:
                            body = await page.inner_text("body")
                        except Exception as e:
                            if is_dead(e):
                                break
                            await z_sleep(1)
                            continue
                        lb = body.lower()
                        if "too many" in lb or "slow down" in lb:
                            await z_sleep(2)
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        if "please wait" in lb and ("minute" in lb or "second" in lb):
                            wt = parse_wait_time(body)
                            if wt <= 0: wt = 60
                            wt += 3
                            for r in range(wt, 0, -1):
                                if session.stop_event.is_set():
                                    break
                                m, s = r // 60, r % 60
                                session.set_countdown(f" {m}m {s:02d}s" if m else f" {s}s")
                                await z_sleep(1)
                            session.set_countdown("")
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(1)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        if "ready" in lb and "next submit" in lb:
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            await page.locator(submit_panel_sel).first.click()
                            await z_sleep(3)
                            continue
                        if "successfully" in lb:
                            count = 0
                            for line in body.split('\n'):
                                if 'successfully' in line.lower():
                                    nums = [int(m) for m in re.findall(r'\d+', line) if not (2020 <= int(m) <= 2035) and int(m) < 100000]
                                    if nums:
                                        count = max(nums)
                                    break
                            new_total = session.add_count(count)
                            session.log(f" +{count} {unit}! Total: {new_total:,}")
                            break
                        send_clicked = False
                        try:
                            await remove_overlays_async(page)
                            await z_sleep(0.3)
                            sels = []
                            if panel_sel:
                                sels += [f'{panel_sel} {HEARTS_BTN_SEL}:visible', f'{panel_sel} button.btn-dark:visible', f'{panel_sel} button.wbutton:visible']
                            sels += [f'{HEARTS_BTN_SEL}:visible', 'button.btn-dark:visible', 'button.wbutton:visible']
                            for sel in sels:
                                try:
                                    btn = page.locator(sel).first
                                    if await btn.is_visible(timeout=1000):
                                        await btn.click()
                                        send_clicked = True
                                        await z_sleep(3)
                                        break
                                except:
                                    continue
                            if not send_clicked:
                                send_clicked = await page.evaluate("""() => {
                                    for (const b of document.querySelectorAll('button')) {
                                        if ((b.className.includes('btn-dark') || b.className.includes('wbutton')) && b.offsetWidth > 0) {
                                            b.click(); return true;
                                        }
                                    }
                                    return false;
                                }""")
                                if send_clicked:
                                    await z_sleep(3)
                            if send_clicked:
                                continue
                        except:
                            pass
                        if check_i < 30:
                            await z_sleep(1)
                        else:
                            break
                    await z_sleep(2)
                    if cycle % 10 == 0:
                        _gc.collect()

            except Exception as e:
                if not is_dead(e):
                    session.log(f" Error: {e}")
            finally:
                if cam_task is not None:
                    cam_task.cancel()
                if not page_closed:
                    try:
                        await page.close()
                    except:
                        pass
        session.log(" Page exhausted restarts.")
    except Exception as e:
        session.log(f" Page fatal: {e}")
    finally:
        with session.count_lock:
            session.active_tabs = max(0, session.active_tabs - 1)
            if session.active_tabs <= 0 and session.status == "running":
                session.status = "error"


async def _run_shared_browser_async(session, browser_idx):
    """Launch one Chromium that handles multiple Zefoy pages (async)."""
    import gc as _gc
    start_id = browser_idx * PAGES_PER_BROWSER
    end_id = min(start_id + PAGES_PER_BROWSER, session.num_tabs)
    tab_ids = list(range(start_id, end_id))
    if not tab_ids:
        return

    session.log(f" [B{browser_idx+1}] Launching browser for {len(tab_ids)} pages...")
    got_slot = False
    browser = None
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _browser_semaphore.acquire)
        got_slot = True
        with _active_browsers_lock:
            global _active_browsers
            _active_browsers += 1
    except:
        pass

    try:
        async with async_pw() as p:
            launch_opts = {
                "headless": True,
                "args": [
                    "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                    "--disable-extensions", "--disable-background-networking",
                    "--disable-default-apps", "--disable-sync", "--disable-translate",
                    "--no-first-run", "--disable-background-timer-throttling",
                    "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-features=TranslateUI", "--renderer-process-limit=1",
                    "--js-flags=--max-old-space-size=128", "--disable-software-rasterizer",
                    "--disable-logging", "--disable-hang-monitor",
                    "--disable-ipc-flooding-protection", "--memory-pressure-off",
                ],
            }
            if USING_TOR:
                tor_port = 9050 + (browser_idx % 10)
                for _tw in range(60):
                    if os.path.exists("/tmp/tor_ready"):
                        break
                    if _tw == 0:
                        session.log(" Waiting for Tor...")
                    await asyncio.sleep(1)
                launch_opts["proxy"] = {"server": f"socks5://127.0.0.1:{tor_port}"}
            elif PROXY_URL:
                launch_opts["proxy"] = {"server": PROXY_URL}

            # Launch Chromium with retries: transient driver failures (thread/process
            # pressure, "Connection closed while reading from the driver") are common
            # under concurrency, so retry with backoff before giving up.
            launch_err = None
            for launch_attempt in range(3):
                try:
                    browser = await p.chromium.launch(slow_mo=100, **launch_opts)
                    launch_err = None
                    break
                except Exception as le:
                    launch_err = le
                    _gc.collect()
                    session.log(f" [B{browser_idx+1}] Launch attempt {launch_attempt+1}/3 failed: {le} (retrying...)")
                    await asyncio.sleep(5 * (launch_attempt + 1))
            if launch_err is not None:
                raise launch_err
            
            # Each _run_page_async creates its own pages from the shared browser
            tasks = []
            for tid in tab_ids:
                tasks.append(asyncio.create_task(_run_page_async(session, browser, tid, browser_idx)))
                await asyncio.sleep(2)
            
            await asyncio.gather(*tasks)
    except Exception as e:
        session.log(f" [B{browser_idx+1}] Browser error: {e}")
    finally:
        try:
            if browser:
                await browser.close()
        except:
            pass
        if got_slot:
            with _active_browsers_lock:
                _active_browsers = max(0, _active_browsers - 1)
            _browser_semaphore.release()
            got_slot = False
        _gc.collect()
    session.log(f" [B{browser_idx+1}] Browser finished")

# 
#  ROUTES
# 
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/tor-status")
def tor_status():
    import socket, subprocess as sp
    ready = os.path.exists("/tmp/tor_ready")
    log = ""
    try:
        with open("/tmp/tor.log") as f:
            log = f.read()[-3000:]
    except:
        log = "No log file yet"
    ports = {}
    for port in range(9050, 9060):
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
            s.close()
            ports[port] = "OPEN"
        except:
            ports[port] = "CLOSED"
    try:
        result = sp.run(["pgrep", "-a", "tor"], capture_output=True, text=True, timeout=3)
        tor_procs = result.stdout.strip()
    except:
        tor_procs = "unknown"
    return jsonify({"ready": ready, "ports": ports, "processes": tor_procs, "log": log})

@app.route("/sessions")
def list_sessions():
    with sessions_lock:
        data = [s.to_dict() for s in sessions.values()]
    return jsonify({"sessions": data, "browsers": _active_browsers, "maxBrowsers": MAX_GLOBAL_BROWSERS, "captchaConcurrency": CAPTCHA_CONCURRENCY})

@app.route("/start", methods=["POST"])
def start():
    data = request.get_json()
    url = data.get("url", "").strip()
    service = data.get("service", "views").strip().lower()
    username = data.get("username", "").strip()
    tabs = int(data.get("tabs", 1))
    tabs = max(1, min(tabs, MAX_GLOBAL_BROWSERS * PAGES_PER_BROWSER))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if service not in SERVICES:
        return jsonify({"error": f"Unknown service: {service}"}), 400
    if service == "comment_hearts" and not username:
        return jsonify({"error": "Username is required for Comment Hearts"}), 400
    session = Session(url, service=service, num_tabs=tabs, username=username)
    with sessions_lock:
        sessions[session.id] = session
    t = threading.Thread(target=run_session, args=(session,), daemon=True)
    session.thread = t
    t.start()
    return jsonify(session.to_dict())

@app.route("/stop/<int:sid>", methods=["POST"])
def stop(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if not session:
        return jsonify({"error": "Not found"}), 404
    session.stop_event.set()
    session.status = "stopping"
    return jsonify({"ok": True})

@app.route("/stream/all")
def stream_all():
    def generate():
        tracking = {}
        while True:
            with sessions_lock:
                current_sessions = dict(sessions)
            for sid, session in current_sessions.items():
                if sid not in tracking:
                    tracking[sid] = {"last_log_idx": 0, "last_countdown": "", "ended_sent": False}
                t = tracking[sid]
                current_len = len(session.logs)
                while t["last_log_idx"] < current_len:
                    data = json.dumps({"type": "log", "sid": sid, "text": session.logs[t["last_log_idx"]]})
                    yield f"data: {data}\n\n"
                    t["last_log_idx"] += 1
                cd = session.countdown
                if cd != t["last_countdown"]:
                    t["last_countdown"] = cd
                    data = json.dumps({"type": "countdown", "sid": sid, "text": cd})
                    yield f"data: {data}\n\n"
                data = json.dumps({
                    "type": "stats",
                    "sid": sid,
                    "count": session.total_count,
                    "unit": session.svc["unit"],
                    "cycles": session.cycles,
                    "status": session.status,
                })
                yield f"data: {data}\n\n"
                if session.status in ("stopped", "error") and not t["ended_sent"]:
                    data = json.dumps({"type": "ended", "sid": sid, "status": session.status})
                    yield f"data: {data}\n\n"
                    t["ended_sent"] = True
            tracking = {sid: v for sid, v in tracking.items() if sid in current_sessions}
            time.sleep(0.5)
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )

_placeholder_frame = None

def _get_placeholder_frame():
    # Dark placeholder JPEG shown while waiting for the first real frame
    global _placeholder_frame
    if _placeholder_frame is None:
        try:
            buf = BytesIO()
            img = Image.new("RGB", (160, 120), (8, 8, 18))
            img.save(buf, format="JPEG", quality=40)
            _placeholder_frame = buf.getvalue()
        except Exception:
            _placeholder_frame = b""
    return _placeholder_frame

@app.route("/stream/video/<int:sid>/<int:tab_id>")
def stream_video(sid, tab_id):
    with sessions_lock:
        session = sessions.get(sid)
    if not session or tab_id not in session.video_buffers:
        return "Video not available", 404
    frame = session.video_buffers[tab_id].get_latest()
    if not frame:
        frame = _get_placeholder_frame()
    return Response(frame, mimetype="image/jpeg", headers={"Cache-Control": "no-cache"})

@app.route("/tabs/<int:sid>")
def get_tabs(sid):
    with sessions_lock:
        session = sessions.get(sid)
    if not session:
        return jsonify({"error": "Not found"}), 404
    tabs = list(session.video_buffers.keys())
    return jsonify({"tabs": tabs, "num_tabs": session.num_tabs})

@app.route("/remove/<int:sid>", methods=["POST"])
def remove_session(sid):
    with sessions_lock:
        session = sessions.get(sid)
        if not session:
            return jsonify({"error": "Not found"}), 404
        session.stop_event.set()
        session.status = "stopping"
        del sessions[sid]
    return jsonify({"ok": True})

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=8080)
