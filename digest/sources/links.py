from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from ..config import Config
from ..llm import LLM
from ..media import MEDIA_NAME, MIME_FOR_VISION, cover_candidates, store_image, try_fetch_image
from ..models import Doc
from ..recipes import recipe_from_jsonld
from .documents import DOC_SCHEME, PdfError, extract_pdf, pdf_doc
from ..state import State
from ..util import normalize_ws, truncate

log = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/129.0.0.0 Safari/537.36")
URL_RE = re.compile(r"https?://\S+")
VIDEO_HOSTS = ("youtube.com", "youtu.be", "tiktok.com", "vimeo.com")
IMAGE_PROMPT = ("Descrivi brevemente questa immagine di un post social e trascrivi fedelmente tutto il testo "
                "visibile (slide, didascalie, grafici). Rispondi in italiano.")

# 20261003 ++ RG #instagram_carousel
MAX_CAROUSEL_SLIDES = 20
INSTAGRAM_NEXT_SELECTOR = 'button[aria-label="Next"], button[aria-label="Avanti"]'
# 20261004 ++ RG #docker the container drops every capability, so Chromium cannot build its own sandbox
CHROMIUM_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]
# 20261004 ** RG #instagram_logged_out the logged-out layout has no <article>: pick the big images instead
INSTAGRAM_SLIDES_JS = """() => {
    const big = [...document.querySelectorAll('img')].filter(img => img.naturalWidth >= 320);
    const inArticle = big.filter(img => img.closest('article'));
    return (inArticle.length ? inArticle : big).map(img => img.currentSrc || img.src);
}"""
INSTAGRAM_COOKIE_CONSENT = re.compile(r"Rifiuta cookie facoltativi|Decline optional cookies", re.I)


# 20261002 ++ RG #screenshots uploaded screenshots live in the same queue under an internal image:// reference
IMAGE_SCHEME = "image://"
SCREENSHOT_PROMPT = ("Questo è uno screenshot salvato dall'utente. Trascrivi fedelmente tutto il testo visibile, "
                     "mantenendo ordine ed elenchi (ingredienti, passaggi, numeri, nomi), poi descrivi in breve cosa "
                     "mostra. Non inventare testo che non vedi. Rispondi in italiano.")


def screenshot_doc(cfg: Config, llm: LLM | None, url: str, note: str) -> Doc:
    name = url.removeprefix(IMAGE_SCHEME)
    if not MEDIA_NAME.match(name):
        raise ValueError("invalid screenshot reference")
    path = cfg.data_path / "media" / name
    if not path.is_file():
        raise FileNotFoundError("the stored screenshot is missing")
    if llm is None:
        description = "[dry-run: the vision model was not called]"
    else:
        if not cfg.llm.vision_model:
            raise ValueError("per leggere gli screenshot imposta [llm] vision_model (es. gemma3:4b)")
        description = llm.describe_image(path.read_bytes(), MIME_FOR_VISION[name.rsplit(".", 1)[1]],
                                         SCREENSHOT_PROMPT, max_tokens=cfg.llm.max_tokens)
        if len(description.strip()) < 20:
            raise ValueError("il modello non ha letto nulla nello screenshot")
    header = ["Tipo: screenshot caricato dall'utente"] + ([f"Nota dell'utente: {note}"] if note else [])
    return Doc(source="link", id=url, title=note or "Screenshot", url=url,
               text="\n".join(header) + "\n\n[Contenuto dello screenshot]\n" + description,
               meta={"note": note, "platform": "screenshot", "method": ["vision"], "image": name})


def read_links_file(path: Path) -> list[tuple[str, str]]:
    """Format: one URL per line, optional note after the URL. Lines starting with # are comments."""
    out: list[tuple[str, str]] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = URL_RE.search(line)
        if m:
            note = (line[: m.start()] + line[m.end():]).strip(" -—|\t")
            out.append((m.group(0).rstrip(").,;"), note))
    return out


def classify(url: str) -> str:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("threads.net") or host.endswith("threads.com"):
        return "threads"
    if any(host.endswith(h) for h in VIDEO_HOSTS):
        return "video"
    return "web"


@dataclass
class _YtdlpLogger:

    def debug(self, msg):
        log.debug("yt-dlp: %s", msg)

    info = debug

    def warning(self, msg):
        log.debug("yt-dlp: %s", msg)

    def error(self, msg):
        log.debug("yt-dlp: %s", msg)


@dataclass
class Fetched:
    title: str = ""
    author: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    cover_candidates: list[str] = field(default_factory=list)
    ytdlp_covers: list[str] = field(default_factory=list)
    recipe: dict | None = None
    is_video: bool = False
    error: str = ""
    method: list[str] = field(default_factory=list)


def og_meta(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "lxml")
    meta: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = tag.get("property") or tag.get("name")
        if key and tag.get("content") and key.lower() not in meta:
            meta[key.lower()] = tag["content"]
    if soup.title and soup.title.string:
        meta.setdefault("title", soup.title.string.strip())
    return meta


class LinkFetcher:
    def __init__(self, cfg: Config, llm: LLM | None, http: httpx.Client | None = None):
        self.cfg = cfg
        self.lc = cfg.links
        self.llm = llm
        self.http = http or httpx.Client(timeout=30, follow_redirects=True,
                                         headers={"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"})

    def _get(self, url: str) -> httpx.Response:
        r = self.http.get(url)
        r.raise_for_status()
        return r

    # 20261004 ** RG #cookies_file_wins no browser profile inside a container: an explicit cookies.txt beats the browser
    def _ytdlp_cookie_opts(self) -> dict:
        if self.lc.cookies_file:
            return {"cookiefile": os.path.expanduser(self.lc.cookies_file)}
        if self.lc.cookies_from_browser:
            return {"cookiesfrombrowser": (self.lc.cookies_from_browser,)}
        return {}

    def _ytdlp_info(self, url: str) -> dict | None:
        if not self.lc.use_ytdlp:
            return None
        try:
            import yt_dlp
        except ImportError:
            log.debug("yt-dlp non installato")
            return None
        opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
                "logger": _YtdlpLogger()}
        opts.update(self._ytdlp_cookie_opts())
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception as e:  # yt-dlp raises many different exception types
            log.info("yt-dlp non è riuscito su %s: %s", url, str(e).splitlines()[0][:200])
            return None

    def _transcribe(self, url: str) -> str:
        if not self.lc.transcribe:
            return ""
        try:
            import yt_dlp
            from faster_whisper import WhisperModel
        except ImportError:
            log.warning("Trascrizione attiva ma mancano yt-dlp/faster-whisper (pip install '.[transcribe]')")
            return ""
        with tempfile.TemporaryDirectory() as tmp:
            opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best", "noplaylist": True,
                    "outtmpl": str(Path(tmp) / "audio.%(ext)s"), "logger": _YtdlpLogger()}
            opts.update(self._ytdlp_cookie_opts())
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as e:
                log.info("Download audio fallito per %s: %s", url, str(e)[:200])
                return ""
            files = list(Path(tmp).glob("audio.*"))
            if not files:
                return ""
            model = WhisperModel(self.lc.whisper_model, device="auto", compute_type="int8")
            segments, _ = model.transcribe(str(files[0]), vad_filter=True)
            return " ".join(s.text.strip() for s in segments)

    def _render(self, url: str) -> str | None:
        if not self.lc.use_playwright:
            return None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("use_playwright attivo ma playwright non installato (pip install '.[render]')")
            return None
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            try:
                page = browser.new_page(user_agent=UA, locale="it-IT")
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
                return page.content()
            finally:
                browser.close()

    # 20261003 ++ RG #instagram_carousel
    def _browser_cookies(self) -> list[dict]:
        if not (self.lc.cookies_from_browser or self.lc.cookies_file):
            return []
        try:
            if self.lc.cookies_file:
                jar = MozillaCookieJar(os.path.expanduser(self.lc.cookies_file))
                jar.load(ignore_discard=True, ignore_expires=True)
            else:
                from yt_dlp.cookies import extract_cookies_from_browser
                jar = extract_cookies_from_browser(self.lc.cookies_from_browser)
        except Exception as e:  # keychain denied, browser missing, unsupported profile, bad cookies file...
            log.info("Cookie non leggibili: %s", str(e)[:200])
            return []
        return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path, "secure": bool(c.secure),
                 "expires": c.expires if c.expires else -1}
                for c in jar if "instagram.com" in c.domain]

    # 20261003 ++ RG #instagram_carousel
    def _instagram_slides(self, url: str) -> list[str]:
        """Walk an image carousel with a headless browser and return every slide URL, in order."""
        if not self.lc.use_playwright:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.warning("use_playwright attivo ma playwright non installato (pip install '.[render]')")
            return []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=CHROMIUM_ARGS)
            page = None
            try:
                context = browser.new_context(user_agent=UA, locale="it-IT")
                cookies = self._browser_cookies()
                # 20261004 ++ RG #instagram_debug
                log.info("Cookie Instagram caricati: %d (sessionid: %s)", len(cookies),
                         any(c["name"] == "sessionid" for c in cookies))
                if cookies:
                    context.add_cookies(cookies)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                self._dismiss_instagram_consent(page)
                page.wait_for_function(f"({INSTAGRAM_SLIDES_JS})().length > 0", timeout=15000)
                slides: dict[str, str] = {}
                for _ in range(MAX_CAROUSEL_SLIDES):
                    for src in page.evaluate(INSTAGRAM_SLIDES_JS):
                        slides.setdefault(src.split("?")[0], src)
                    next_button = page.query_selector(INSTAGRAM_NEXT_SELECTOR)
                    if not next_button:
                        break
                    next_button.click()
                    page.wait_for_timeout(400)
                return list(slides.values())
            except Exception as e:  # login wall, timeout, layout change: fall back to the cover image
                log.info("Carosello Instagram non letto su %s: %s", url, str(e).splitlines()[0][:200])
                # 20261004 ++ RG #instagram_debug tell a login wall from a layout change
                self._dump_instagram_debug(page)
                return []
            finally:
                browser.close()

    @staticmethod
    def _dismiss_instagram_consent(page) -> None:
        try:
            page.get_by_text(INSTAGRAM_COOKIE_CONSENT).first.click(timeout=4000)
        except Exception:  # no banner for this session
            pass

    def _dump_instagram_debug(self, page) -> None:
        if page is None:
            return
        try:
            log.info("Instagram debug: url=%s title=%r", page.url, page.title())
            page.screenshot(path=str(self.cfg.data_path / "instagram_debug.png"))
        except Exception as e:
            log.info("Instagram debug non disponibile: %s", str(e)[:120])

    def _describe_images(self, urls: list[str], limit: int = 4) -> list[str]:
        if not (self.llm and self.cfg.llm.vision_model):
            return []
        out = []
        for u in urls[:limit]:
            # 20261002 ** RG #images guarded download (SSRF, size cap, magic bytes) instead of a plain GET
            fetched = try_fetch_image(self.http, u, self.lc.max_image_bytes)
            if not fetched:
                continue
            data, extension = fetched
            try:
                desc = self.llm.describe_image(data, MIME_FOR_VISION[extension], IMAGE_PROMPT)
            except Exception as e:
                log.info("Immagine non analizzabile %s: %s", u, e)
                continue
            if desc:
                out.append(desc)
        return out

    def store_cover(self, f: Fetched) -> str | None:
        """Download the best available cover image and keep it in data_dir/media. Never fails the link."""
        for url in [*f.cover_candidates, *f.ytdlp_covers]:
            fetched = try_fetch_image(self.http, url, self.lc.max_image_bytes)
            if fetched:
                return store_image(self.cfg.data_path / "media", *fetched)
        return None

    def _from_ytdlp(self, f: Fetched, info: dict) -> None:
        f.title = f.title or info.get("title") or ""
        f.author = f.author or info.get("uploader") or info.get("channel") or ""
        desc = info.get("description") or ""
        if desc:
            f.text += desc + "\n"
        thumbs = [t.get("url") for t in (info.get("thumbnails") or []) if t.get("url")]
        if info.get("thumbnail"):
            thumbs.insert(0, info["thumbnail"])
        f.images += thumbs[:1]
        f.ytdlp_covers += thumbs[:1]
        f.is_video = f.is_video or bool(info.get("duration")) or info.get("vcodec") not in (None, "none")
        f.method.append("yt-dlp")

    def _from_html(self, f: Fetched, html: str, url: str, generic: bool) -> None:
        meta = og_meta(html)
        f.title = f.title or meta.get("og:title") or meta.get("twitter:title") or meta.get("title", "")
        f.author = f.author or meta.get("author") or meta.get("article:author") or ""
        if meta.get("og:image"):
            f.images.append(meta["og:image"])
        f.cover_candidates += [c for c in cover_candidates(html, url, meta) if c not in f.cover_candidates]
        f.recipe = f.recipe or recipe_from_jsonld(html)
        if (meta.get("og:type") or "").startswith("video"):
            f.is_video = True
        body = ""
        if generic:
            try:
                import trafilatura
                body = trafilatura.extract(html, url=url, include_comments=False, include_tables=True,
                                           favor_recall=True) or ""
            except ImportError:
                pass
        if len(body) < 200:
            desc = meta.get("og:description") or meta.get("description") or meta.get("twitter:description") or ""
            if desc and desc not in f.text:
                body = (desc + "\n\n" + body).strip()
        if body and body not in f.text:
            f.text += body + "\n"
        f.method.append("html")

    def fetch(self, url: str) -> Fetched:
        kind = classify(url)
        f = Fetched(is_video=kind == "video")

        if kind in ("instagram", "video"):
            info = self._ytdlp_info(url)
            if info:
                self._from_ytdlp(f, info)

        # 20261003 ++ RG #instagram_carousel image-only posts have no video, yt-dlp cannot read them
        slides = self._instagram_slides(url) if kind == "instagram" and not f.method else []

        if len(f.text) < 80:
            try:
                r = self._get(url)
                ctype = r.headers.get("content-type", "")
                if "application/pdf" in ctype:
                    if len(r.content) > self.lc.max_pdf_bytes:
                        raise PdfError(f"PDF troppo grande (max {self.lc.max_pdf_bytes // 1_000_000} MB)")
                    pdf = extract_pdf(r.content, self.cfg, self.llm)
                    f.text += pdf.text
                    f.title = f.title or pdf.title
                    f.method += ["pdf"] + (["vision"] if pdf.scanned_pages else [])
                else:
                    self._from_html(f, r.text, str(r.url), generic=kind in ("web", "video"))
            except (httpx.HTTPError, PdfError) as e:
                f.error = str(e).splitlines()[0]
                log.info("Download diretto fallito %s: %s", url, f.error)

        if len(f.text) < 200 and self.lc.use_playwright:
            html = self._render(url)
            if html:
                if kind == "web":
                    self._from_html(f, html, url, generic=True)
                else:
                    # social: the rendered visible text beats the meta tags
                    visible = normalize_ws(BeautifulSoup(html, "lxml").get_text("\n"))
                    f.text += "\n" + truncate(visible, 6000)
                    f.method.append("playwright")

        if f.is_video:
            transcript = self._transcribe(url)
            if transcript:
                f.text += f"\n[Trascrizione audio]\n{transcript}\n"
                f.method.append("whisper")

        if kind in ("instagram", "threads") or (len(f.text) < 300 and f.images):
            # 20261003 ** RG #instagram_carousel describe every slide instead of the cover only
            images = list(dict.fromkeys(slides or f.images))
            for i, d in enumerate(self._describe_images(images, MAX_CAROUSEL_SLIDES if slides else 4), 1):
                f.text += f"\n[Immagine {i}]\n{d}\n"
                f.method.append("vision")

        f.text = normalize_ws(f.text)
        return f


def sync_links(cfg: Config, state: State) -> int:
    added = 0
    for url, note in read_links_file(cfg.links_path):
        added += state.add_link(url, note)
    return added


def fetch_links(cfg: Config, state: State, llm: LLM | None, http: httpx.Client | None = None,
                store_images: bool = True) -> tuple[list[Doc], list[str]]:
    """Return the pending links' Docs and the errors (failed links stay queued for later runs)."""
    sync_links(cfg, state)
    fetcher = LinkFetcher(cfg, llm, http)
    docs, errors = [], []
    for url, note in state.pending_links(cfg.links.max_attempts):
        if url.startswith((IMAGE_SCHEME, DOC_SCHEME)):
            builder = screenshot_doc if url.startswith(IMAGE_SCHEME) else pdf_doc
            try:
                docs.append(builder(cfg, llm, url, note))
            except Exception as e:
                log.info("Contenuto locale %s non leggibile: %s", url, e)
                state.link_failed(url, str(e))
                errors.append(f"{url}: {e}")
            continue
        try:
            f = fetcher.fetch(url)
        except Exception as e:
            log.exception("Errore su %s", url)
            state.link_failed(url, str(e))
            errors.append(f"{url}: {e}")
            continue
        if len(f.text.strip()) < 20:
            msg = f.error or ("contenuto non estraibile (post privato o login richiesto? "
                              "prova cookies_from_browser o use_playwright)")
            state.link_failed(url, msg)
            errors.append(f"{url}: {msg}")
            continue
        header = [f"URL: {url}", f"Piattaforma: {classify(url)}"]
        if f.title:
            header.append(f"Titolo: {f.title}")
        if f.author:
            header.append(f"Autore: {f.author}")
        if note:
            header.append(f"Nota dell'utente: {note}")
        image = fetcher.store_cover(f) if store_images and cfg.links.fetch_images else None
        docs.append(Doc(source="link", id=url, title=f.title or url, url=url,
                        text="\n".join(header) + "\n\n" + truncate(f.text, cfg.links.max_chars),
                        meta={"note": note, "platform": classify(url), "method": f.method, "image": image,
                              "recipe_jsonld": f.recipe}))
    return docs, errors
