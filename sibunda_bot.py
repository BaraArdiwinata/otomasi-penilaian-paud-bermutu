"""Otomasi SIBUNDA: login, telusuri indikator, ambil tautan berkas, isi form."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError

from ai_evaluator import DISETUJUI, Evaluation
from config import Settings

log = logging.getLogger(__name__)

CARD_ATTR = "data-sibunda-card"

# Halaman verifikasi tidak punya markup stabil antar-indikator, jadi kartu
# indikator dicari dari tombol "Buka File" lalu naik ke induk terdekat yang
# punya kontrol penilaian.
_DISCOVER_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const isBuka = (el) => /buka\s*file/i.test(norm(el.innerText || el.textContent));
  document.querySelectorAll('[data-sibunda-card]').forEach((el) => el.removeAttribute('data-sibunda-card'));
  const controls = Array.from(
    document.querySelectorAll('a, button, [role="button"], input[type="button"]')
  ).filter((el) => isBuka(el) && !el.disabled);

  const seen = new Set();
  const items = [];

  const isLabelNoise = (text) => /^(skor|nilai|status|disetujui|ditolak|pending|bukti|berkas|aksi|\d+)$/i.test(text);

  const pickTitle = (card, button) => {
    const nodes = card.querySelectorAll(
      'h1, h2, h3, h4, h5, h6, .card-title, .card-header, .fw-bold, .fw-semibold, strong, b, td'
    );
    for (const node of nodes) {
      if (node === button || node.contains(button) || button.contains(node)) continue;
      const text = norm(node.innerText);
      if (text.length >= 8 && !isBuka(node) && !isLabelNoise(text)) return text.slice(0, 300);
    }
    return norm(card.innerText).replace(/buka\s*file/ig, ' ').trim().slice(0, 300);
  };

  const COUNT_SELECTOR = 'a, button, [role="button"], input[type="button"]';
  const bukaCount = (node) =>
    Array.from(node.querySelectorAll(COUNT_SELECTOR)).filter(isBuka).length;
  const CONTROL_SELECTOR =
    'input[type="radio"], select, textarea, input[id^="notes"], ' +
    'button[onclick*="setDocScore"], button[onclick*="saveDocument"]';

  // Kartu = induk yang memuat kontrol penilaian sekaligus judul indikator,
  // tanpa menyerap tombol "Buka File" milik indikator lain.
  const pickCard = (button) => {
    let node = button.parentElement;
    let fallback = null;
    let depth = 0;
    while (node && depth < 12) {
      if (bukaCount(node) > 1) break;
      if (node.querySelector(CONTROL_SELECTOR)) {
        if (node.querySelector('h1, h2, h3, h4, h5, h6')) return node;
        if (!fallback) fallback = node;
      }
      node = node.parentElement;
      depth += 1;
    }
    return fallback || button.parentElement;
  };

  for (const button of controls) {
    const card = pickCard(button);
    if (!card || seen.has(card)) continue;
    seen.add(card);

    const radios = Array.from(card.querySelectorAll('input[type="radio"]'));
    const selects = Array.from(card.querySelectorAll('select'));
    const textareas = Array.from(card.querySelectorAll('textarea, [contenteditable="true"]'));
    const scoreButtons = Array.from(card.querySelectorAll('button[onclick*="setDocScore"]'));
    const notesInputs = Array.from(
      card.querySelectorAll('input[id^="notes"], input[name*="notes" i], input[placeholder*="catatan" i]')
    );

    const scores = scoreButtons.map((btn) => {
      const match = (btn.getAttribute('onclick') || '').match(/setDocScore\(\s*\d+\s*,\s*(\d+)\s*\)/);
      return match ? parseInt(match[1], 10) : null;
    });

    let activeScore = null;
    scoreButtons.forEach((btn, position) => {
      // Kelas aktif berbeda per skor: rose/amber/blue/emerald + text-white.
      const cls = btn.className || '';
      const highlighted =
        /text-white/.test(cls) &&
        /(^|\s)bg-(rose|amber|blue|emerald|brand|green|teal|cyan|orange)-\d{3}/.test(cls);
      if (highlighted && scores[position] !== null && scores[position] !== undefined) {
        activeScore = scores[position];
      }
    });

    const noteRaw = notesInputs.length
      ? norm(notesInputs[0].value)
      : (textareas.length ? norm(textareas[0].value || textareas[0].innerText) : '');
    const selectValue = selects.length ? (selects[0].value || '') : '';
    const checked = radios.some((radio) => radio.checked);
    const href = button.getAttribute('href') || '';

    items.push({
      index: items.length,
      name: pickTitle(card, button),
      text: norm(card.innerText).slice(0, 1500),
      href: /^javascript:/i.test(href) ? '' : href.trim(),
      has_score: radios.length > 0 || scoreButtons.length > 0,
      has_status: selects.length > 0,
      has_note: textareas.length > 0 || notesInputs.length > 0,
      filled_score: activeScore !== null && activeScore !== undefined ? activeScore : (checked ? 1 : null),
      filled_status: selectValue,
      filled_note: noteRaw,
      already_filled:
        checked ||
        (activeScore !== null && activeScore !== undefined) ||
        (noteRaw !== '' && noteRaw !== '-') ||
        (selectValue !== '' && selectValue.toLowerCase() !== 'pending'),
    });
    card.setAttribute('data-sibunda-card', String(items.length - 1));
  }

  return items;
}
"""

# Tombol skor aktif ditandai warna berbeda per nilai (rose, amber, blue, emerald).
_ACTIVE_SCORE_JS = r"""
(el) => {
  const buttons = Array.from(el.querySelectorAll('button[onclick*="setDocScore"]'));
  for (const btn of buttons) {
    const match = (btn.getAttribute('onclick') || '').match(/setDocScore\(\s*\d+\s*,\s*(\d+)\s*\)/);
    const cls = btn.className || '';
    const active =
      /text-white/.test(cls) &&
      /(^|\s)bg-(rose|amber|blue|emerald|brand|green|teal|cyan|orange)-\d{3}/.test(cls);
    if (match && active) return parseInt(match[1], 10);
  }

  const checked = el.querySelector('input[type="radio"]:checked');
  if (checked && checked.value) return parseInt(checked.value, 10);

  const pressed = el.querySelector('button[aria-pressed="true"]');
  if (pressed) {
    const match = (pressed.getAttribute('onclick') || '').match(/setDocScore\(\s*\d+\s*,\s*(\d+)\s*\)/);
    if (match) return parseInt(match[1], 10);
  }
  return null;
}
"""


class SibundaError(RuntimeError):
    """Halaman SIBUNDA tidak sesuai harapan."""


class LoginError(SibundaError):
    """Login gagal atau terhalang."""


class NoDocumentError(SibundaError):
    """Berkas bukti belum diunggah atau tautannya tidak bisa dibuka."""


@dataclass
class WebIndicator:
    index: int
    name: str
    raw_text: str = ""
    drive_url: str | None = None
    has_score: bool = False
    has_status: bool = False
    has_note: bool = False
    already_filled: bool = False
    filled_score: int | None = None
    filled_status: str = ""
    filled_note: str = ""

    @property
    def selector(self) -> str:
        return f'[{CARD_ATTR}="{self.index}"]'

    @property
    def key(self) -> str:
        return re.sub(r"\s+", " ", (self.name or "").strip().lower())


@dataclass
class SaveResult:
    ok: bool
    message: str = ""
    filled: dict[str, bool] | None = None


class SibundaBot:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._pw: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self._logged_in = False

    async def __aenter__(self) -> "SibundaBot":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        launch_options: dict = {
            "headless": self.settings.headless,
            "slow_mo": self.settings.slow_mo_ms,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        self.browser = await self._pw.chromium.launch(**launch_options)
        log.info(
            "browser: headless=%s, slow_mo=%s ms",
            self.settings.headless,
            self.settings.slow_mo_ms,
        )

        context_options: dict = {
            "viewport": {"width": 1400, "height": 950},
            "locale": "id-ID",
            "timezone_id": "Asia/Jakarta",
            "accept_downloads": True,
            "ignore_https_errors": True,
        }
        state_path = self._state_path()
        if self.settings.persist_session and state_path.exists():
            context_options["storage_state"] = str(state_path)

        self.context = await self.browser.new_context(**context_options)
        self.page = await self.context.new_page()
        self.page.set_default_timeout(self.settings.action_timeout_ms)
        self.page.set_default_navigation_timeout(self.settings.nav_timeout_ms)
        self.page.on("dialog", self._accept_dialog)

    async def _accept_dialog(self, dialog) -> None:
        log.info("dialog browser: %s", (dialog.message or "")[:200])
        try:
            await dialog.accept()
        except PlaywrightError:
            try:
                await dialog.dismiss()
            except PlaywrightError:
                pass

    async def close(self) -> None:
        try:
            if self.settings.persist_session and self.context and self._logged_in:
                await self.context.storage_state(path=str(self._state_path()))
                log.debug("sesi login disimpan ke %s", self._state_path())
        except Exception as exc:
            log.debug("gagal menyimpan sesi: %s", exc)
        for closer in (
            getattr(self.context, "close", None),
            getattr(self.browser, "close", None),
        ):
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self.context = None
        self.browser = None
        self.page = None
        self._pw = None

    def _state_path(self) -> Path:
        return self.settings.state_dir / "auth.json"

    async def _login_form_visible(self) -> bool:
        assert self.page is not None
        # Halaman dashboard juga punya input bernama "username" di form profil,
        # jadi penanda login harus form login itu sendiri.
        if await self.page.locator("form#loginForm").count():
            return True
        if "/login" not in self.page.url:
            return False
        field = self.page.locator('form#loginForm input[name="username"], input[name="username"]')
        try:
            return await field.first.is_visible()
        except PlaywrightError:
            return False

    async def login(self, force: bool = False) -> None:
        assert self.page is not None
        if self._logged_in and not force:
            return

        await self.page.goto(self.settings.url_login(), wait_until="domcontentloaded")
        if not await self._login_form_visible():
            if "/login" in self.page.url:
                raise LoginError("halaman login SIBUNDA tidak menampilkan form username/password")
            self._logged_in = True
            log.info("sesi SIBUNDA masih aktif")
            return

        self.settings.require_sibunda()
        log.info("login SIBUNDA sebagai %s", self.settings.username)

        await self.page.wait_for_selector(
            'input[name="username"]', state="visible", timeout=self.settings.action_timeout_ms
        )
        await self.page.fill('input[name="username"]', self.settings.username)
        await self.page.fill('input[name="password"]', self.settings.password)

        await self._handle_captcha_if_present()

        submit = self.page.locator(
            'form#loginForm button[type="submit"], button[type="submit"]'
        ).first
        await submit.click()
        await self._settle()

        if "/login" in self.page.url or await self._login_form_visible():
            detail = await self.read_alert()
            raise LoginError(
                "login ke SIBUNDA gagal"
                + (f": {detail}" if detail else " (periksa username/password)")
            )

        self._logged_in = True
        log.info("login berhasil")

    async def _handle_captcha_if_present(self) -> None:
        assert self.page is not None
        captcha = self.page.locator(
            'input[name="captcha"], input[name="kode_captcha"], #captcha'
        ).first
        if not await captcha.count():
            return
        if self.settings.headless:
            raise LoginError(
                "CAPTCHA muncul di halaman login. Jalankan dengan SIBUNDA_HEADLESS=false "
                "lalu selesaikan CAPTCHA secara manual"
            )
        log.warning("CAPTCHA terdeteksi. Selesaikan di jendela browser, lalu tekan Enter di sini")
        await asyncio.to_thread(input, "Tekan Enter setelah CAPTCHA terisi: ")

    async def read_alert(self) -> str:
        assert self.page is not None
        for selector in (
            "#alertBox:not(.hidden)",
            ".alert-danger",
            ".alert",
            "[role=alert]",
            ".invalid-feedback",
        ):
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    text = (await locator.inner_text()).strip()
                    if text:
                        return text[:300]
            except PlaywrightError:
                continue
        return ""

    async def _settle(self, timeout_ms: int | None = None) -> None:
        assert self.page is not None
        timeout = timeout_ms or self.settings.action_timeout_ms
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except PlaywrightTimeoutError:
            log.debug("domcontentloaded tidak selesai dalam %s ms", timeout)
        try:
            await self.page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeoutError:
            log.debug("networkidle tidak selesai dalam %s ms", timeout)

    async def open_verification(self, npsn: str) -> list[WebIndicator]:
        assert self.page is not None
        url = self.settings.url_verifikasi(npsn)
        log.info("membuka %s", url)
        await self.page.goto(url, wait_until="domcontentloaded")

        if "/login" in self.page.url:
            await self.login(force=True)
            await self.page.goto(url, wait_until="domcontentloaded")

        await self._settle()

        if "tahun=" in self.page.url and f"npsn={npsn}" not in self.page.url:
            log.warning("URL tidak memuat npsn, halaman mungkin dialihkan")

        try:
            await self.page.get_by_text(re.compile(r"buka\s*file", re.I)).first.wait_for(
                state="visible", timeout=self.settings.action_timeout_ms
            )
        except PlaywrightTimeoutError as exc:
            preview = (await self.page.content())[:400]
            await self.capture(f"verifikasi-{npsn}")
            raise SibundaError(
                f"tidak ada tombol 'Buka File' pada halaman NPSN {npsn}. "
                f"Cek akses akun atau selector. Cuplikan HTML: {preview!r}"
            ) from exc

        indicators = await self.discover_indicators()
        if not indicators:
            raise SibundaError(f"tidak ada indikator terdeteksi untuk NPSN {npsn}")
        log.info("NPSN %s: %d indikator terdeteksi", npsn, len(indicators))
        return indicators

    async def discover_indicators(self) -> list[WebIndicator]:
        assert self.page is not None
        raw = await self.page.evaluate(_DISCOVER_JS)
        indicators: list[WebIndicator] = []
        for position, item in enumerate(raw or []):
            href = (item.get("href") or "").strip()
            filled_score = item.get("filled_score")
            indicators.append(
                WebIndicator(
                    index=int(item.get("index", position)),
                    name=(item.get("name") or "").strip(),
                    raw_text=item.get("text") or "",
                    drive_url=href if "drive.google.com" in href or "docs.google.com" in href else None,
                    has_score=bool(item.get("has_score")),
                    has_status=bool(item.get("has_status")),
                    has_note=bool(item.get("has_note")),
                    already_filled=bool(item.get("already_filled")),
                    filled_score=int(filled_score) if filled_score else None,
                    filled_status=(item.get("filled_status") or "").strip(),
                    filled_note=(item.get("filled_note") or "").strip(),
                )
            )
        return indicators

    async def redirect_check(self) -> bool:
        """True bila halaman saat ini bukan halaman verifikasi lagi (sesi habis)."""
        assert self.page is not None
        return "/login" in self.page.url or await self._login_form_visible()

    async def pause(self, milliseconds: int) -> None:
        if self.page is not None:
            await self.page.wait_for_timeout(milliseconds)

    async def get_drive_url(self, indicator: WebIndicator) -> str:
        assert self.page is not None
        card = self.page.locator(indicator.selector)
        button = card.locator("a, button, [role=button]").filter(
            has_text=re.compile(r"buka\s*file", re.I)
        ).first

        if not await button.count():
            raise NoDocumentError(
                f"tautan 'Buka File' tidak ada pada indikator: {indicator.name[:80]}"
            )

        href = (await button.get_attribute("href") or "").strip()
        if href.startswith("javascript:"):
            href = ""

        if href:
            absolute = urljoin(self.settings.base_url + "/", href)
            if self._is_drive(absolute):
                return absolute
            if self._looks_like_file(absolute):
                return absolute
            fetched = await self._follow_in_new_tab(absolute)
            if fetched:
                return fetched

        try:
            return await self._resolve_via_click(button)
        except NoDocumentError:
            raise
        except SibundaError as exc:
            raise NoDocumentError(str(exc)) from exc

    @staticmethod
    def _looks_like_file(url: str) -> bool:
        path = urlsplit(url).path
        if re.search(r"\.(pdf|jpe?g|png|gif|bmp|tiff?|docx?|xlsx?|zip)(?:$|\?)", path, re.I):
            return True
        return bool(re.search(r"(unduh|download|/file/|/berkas/|/lampiran/)", path, re.I))

    async def cookies(self) -> dict[str, str]:
        if self.context is None:
            return {}
        try:
            items = await self.context.cookies(self.settings.base_url)
        except PlaywrightError:
            return {}
        return {item["name"]: item["value"] for item in items if item.get("name")}

    @staticmethod
    def _is_drive(url: str) -> bool:
        return "drive.google.com" in url or "docs.google.com" in url

    async def _follow_in_new_tab(self, url: str) -> str | None:
        assert self.context is not None
        temp = await self.context.new_page()
        downloads: list = []
        temp.on("download", lambda download: downloads.append(download))
        try:
            try:
                # "commit" cukup untuk mengetahui URL akhir setelah redirect;
                # menunggu load penuh hanya memperlambat tiap indikator.
                await temp.goto(
                    url, wait_until="commit", timeout=self.settings.action_timeout_ms
                )
            except PlaywrightError as exc:
                # Tautan yang langsung mengunduh membatalkan navigasi (ERR_ABORTED).
                if downloads:
                    return url
                log.debug("gagal membuka tautan di tab sementara: %s", exc)
                return None
            await temp.wait_for_timeout(400)
            resolved = temp.url or url
            if "/login" in resolved:
                return None
            return resolved
        except PlaywrightError as exc:
            log.debug("gagal membuka tautan di tab sementara: %s", exc)
            return None
        finally:
            try:
                await temp.close()
            except Exception:
                pass

    async def _resolve_via_click(self, button) -> str:
        assert self.page is not None
        assert self.context is not None
        downloads: list = []
        handler = lambda download: downloads.append(download)  # noqa: E731
        self.page.on("download", handler)
        try:
            try:
                async with self.context.expect_page(timeout=6_000) as info:
                    await button.click()
                popup = await info.value
            except PlaywrightTimeoutError:
                await self.page.wait_for_timeout(1500)
                if downloads:
                    return downloads[0].url
                for selector in (
                    'a[href*="drive.google.com"]',
                    'a[href*="docs.google.com"]',
                    'iframe[src*="drive.google.com"]',
                ):
                    locator = self.page.locator(selector).last
                    if await locator.count():
                        value = (await locator.get_attribute("href")) or (
                            await locator.get_attribute("src")
                        )
                        if value:
                            return urljoin(self.settings.base_url + "/", value)
                raise SibundaError("klik 'Buka File' tidak membuka tautan Drive")
        finally:
            self.page.remove_listener("download", handler)

        try:
            await popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            await popup.wait_for_timeout(800)
            resolved = popup.url
        except PlaywrightError:
            # Unduhan langsung menutup tab sebelum load state sempat terpenuhi.
            resolved = popup.url
        finally:
            try:
                await popup.close()
            except Exception:
                pass

        if (not resolved or resolved.startswith("about:")) and downloads:
            return downloads[0].url
        if not resolved or resolved.startswith("about:"):
            raise SibundaError("klik 'Buka File' tidak menghasilkan tautan berkas")
        return resolved

    async def fill_and_save(
        self, indicator: WebIndicator, evaluation: Evaluation, dry_run: bool = False
    ) -> SaveResult:
        assert self.page is not None
        card = self.page.locator(indicator.selector)
        if not await card.count():
            raise SibundaError(
                "kartu indikator tidak ditemukan lagi di halaman, kemungkinan halaman reload"
            )

        filled = {"score": False, "status": False, "note": False}
        if dry_run:
            log.info(
                "[dry-run] %s -> skor %s, status %s",
                indicator.name[:70],
                evaluation.skor,
                evaluation.status,
            )
            return SaveResult(ok=True, message="dry-run", filled=filled)

        # Aturan V4: status selalu Disetujui, apa pun keluaran AI.
        status = DISETUJUI
        if evaluation.status != status:
            log.warning("status %r diabaikan, dipaksa %s", evaluation.status, status)

        # Urutan tahan render ulang: skor, tunggu DOM stabil, status, catatan, simpan.
        filled["score"] = await self._set_score(card, evaluation.skor)
        if filled["score"]:
            filled["score"] = await self._confirm_score(indicator, evaluation.skor)
        if not filled["score"]:
            raise SibundaError("skor tidak bisa dipilih atau hilang setelah render ulang")

        card = await self._locate_card(indicator)
        await self._assert_card(card, indicator)
        filled["status"] = await self._set_status(card, status)
        filled["note"] = await self._set_note(card, evaluation.catatan)

        await self._click_save(card)
        ok, message = await self._wait_save_result(filled)
        return SaveResult(ok=ok, message=message, filled=filled)

    async def _locate_card(self, indicator: WebIndicator):
        assert self.page is not None
        try:
            fresh = await self.discover_indicators()
        except PlaywrightError as exc:
            log.debug("gagal menandai ulang kartu: %s", exc)
            return self.page.locator(indicator.selector)
        target = next((item for item in fresh if item.key == indicator.key), None)
        if target is not None:
            indicator.index = target.index
        return self.page.locator(indicator.selector)

    async def _confirm_score(self, indicator: WebIndicator, expected: int) -> bool:
        """Pastikan skor bertahan setelah renderDocuments() membangun ulang daftar."""
        assert self.page is not None
        for attempt in range(1, 3):
            await self.page.wait_for_timeout(self.settings.rerender_wait_ms)
            card = await self._locate_card(indicator)
            try:
                await card.wait_for(state="visible", timeout=self.settings.action_timeout_ms)
                await self._assert_card(card, indicator)
            except (PlaywrightTimeoutError, SibundaError) as exc:
                log.warning("kartu belum siap setelah render ulang (%s)", exc)
                continue

            current = await self._read_active_score(card)
            if current == expected:
                return True
            log.warning(
                "skor terbaca %s, seharusnya %s (percobaan %d), klik ulang",
                current,
                expected,
                attempt,
            )
            if not await self._set_score(card, expected):
                return False
        return False

    async def _read_active_score(self, card) -> int | None:
        try:
            value = await card.evaluate(_ACTIVE_SCORE_JS)
        except PlaywrightError:
            return None
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def _card_title(self, card) -> str:
        heading = card.locator("h3").first
        try:
            if await heading.count():
                return re.sub(r"\s+", " ", (await heading.inner_text())).strip()
        except PlaywrightError:
            pass
        return ""

    async def _assert_card(self, card, indicator: WebIndicator) -> None:
        title = await self._card_title(card)
        if not title:
            return
        actual, expected = self._norm(title), self._norm(indicator.name)
        if actual != expected and expected not in actual and actual not in expected:
            raise SibundaError(
                f"kartu tidak cocok dengan indikator: {title[:60]!r} vs {indicator.name[:60]!r}"
            )

    @staticmethod
    def _norm(value: str) -> str:
        return re.sub(r"\s+", " ", (value or "")).strip().lower()

    async def _set_score(self, card, score: int) -> bool:
        radios = card.locator('input[type="radio"]')
        count = await radios.count()
        for position in range(count):
            radio = radios.nth(position)
            value = (await radio.get_attribute("value") or "").strip()
            if value == str(score) and await self._check_radio(radio):
                return True

        # SIBUNDA: <button onclick="setDocScore(id, 2)">2</button>
        buttons = card.locator('button[onclick*="setDocScore"]')
        for position in range(await buttons.count()):
            button = buttons.nth(position)
            onclick = await button.get_attribute("onclick") or ""
            match = re.search(r"setDocScore\(\s*\d+\s*,\s*(\d+)\s*\)", onclick)
            if match and int(match.group(1)) == score:
                try:
                    await button.click(timeout=5000)
                    return True
                except PlaywrightError as exc:
                    log.warning("gagal klik tombol skor %s: %s", score, exc)

        label = card.locator("label").filter(
            has_text=re.compile(rf"^\s*(?:skor\s*)?{score}\b", re.I)
        ).first
        if await label.count():
            try:
                await label.click(timeout=3000)
                return True
            except PlaywrightError:
                pass

        if count >= score:
            return await self._check_radio(radios.nth(score - 1))
        return False

    async def _check_radio(self, radio) -> bool:
        try:
            if await radio.is_checked():
                return True
        except PlaywrightError:
            pass

        try:
            await radio.check(force=True, timeout=3000)
            return True
        except PlaywrightError:
            pass

        element_id = await radio.get_attribute("id")
        if element_id:
            assert self.page is not None
            label = self.page.locator(f'label[for="{element_id}"]').first
            if await label.count():
                try:
                    await label.click(timeout=3000)
                    return True
                except PlaywrightError:
                    pass

        try:
            # Radio yang disembunyikan CSS tetap bisa diaktifkan lewat event click.
            await radio.evaluate("el => el.click()")
            return True
        except PlaywrightError:
            return False

    async def _set_status(self, card, status: str) -> bool:
        selects = card.locator("select")
        for position in range(await selects.count()):
            select = selects.nth(position)
            options = await select.locator("option").all()
            texts = [(await option.inner_text() or "").strip() for option in options]
            values = [(await option.get_attribute("value") or "").strip() for option in options]
            if not any(
                re.search(r"disetujui|ditolak|pending", f"{text} {value}", re.I)
                for text, value in zip(texts, values)
            ):
                continue

            wanted = self._match_option(texts, values, status)
            if wanted is None:
                continue
            try:
                if values[wanted]:
                    await select.select_option(value=values[wanted])
                else:
                    await select.select_option(index=wanted)
                return True
            except PlaywrightError as exc:
                log.warning("gagal memilih status pada select: %s", exc)

        assert self.page is not None
        option = card.locator("label").filter(
            has_text=re.compile(rf"^\s*{re.escape(status)}\s*$", re.I)
        ).first
        if await option.count():
            try:
                await option.click(timeout=3000)
                return True
            except PlaywrightError:
                pass
        return False

    @staticmethod
    def _match_option(texts: list[str], values: list[str], status: str) -> int | None:
        target = status.strip().lower()
        for index, value in enumerate(values):
            if value.strip().lower() == target:
                return index
        for index, text in enumerate(texts):
            if text.strip().lower() == target:
                return index
        for index, value in enumerate(values):
            if target and target in value.strip().lower():
                return index
        for index, text in enumerate(texts):
            if target and target in text.strip().lower():
                return index
        return None

    async def _set_note(self, card, note: str) -> bool:
        candidates = [
            card.locator("textarea").first,
            card.locator(
                'input[id^="notes"], input[name*="notes" i], input[placeholder*="catatan" i]'
            ).first,
            card.locator('[contenteditable="true"]').first,
        ]
        for target in candidates:
            if not await target.count():
                continue
            try:
                await target.fill(note)
                return True
            except PlaywrightError as exc:
                log.warning("gagal mengisi catatan: %s", exc)
        return False

    async def _click_save(self, card) -> None:
        assert self.page is not None
        selectors = [
            'button[onclick*="saveDocument"]',
            'button:has-text("Simpan Hasil Verifikasi")',
            'button:has-text("Simpan")',
        ]
        for selector in selectors:
            local = card.locator(selector).first
            if await local.count():
                await local.click()
                return

        for selector in selectors[:2]:
            global_button = self.page.locator(selector).first
            if await global_button.count():
                await global_button.click()
                return

        raise SibundaError("tombol 'Simpan Hasil Verifikasi' tidak ditemukan")

    async def _wait_save_result(self, filled: dict[str, bool]) -> tuple[bool, str]:
        assert self.page is not None
        message = ""
        try:
            await self.page.wait_for_selector(".swal2-popup", timeout=15_000)
            title = await self._inner_text(".swal2-title")
            body = await self._inner_text(".swal2-html-container")
            message = f"{title}: {body}".strip(": ").strip()
            confirm = self.page.locator(".swal2-confirm").first
            if await confirm.count():
                await confirm.click(timeout=5000)
            try:
                await self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            await self.pause(self.settings.save_wait_ms)
        except PlaywrightTimeoutError:
            message = await self.read_alert()

        lowered = message.lower()
        if any(word in lowered for word in ("gagal", "error", "kesalahan", "mohon maaf", "500")):
            return False, message or "penyimpanan ditolak"

        note = message or "tersimpan"
        if not filled.get("status"):
            note += " (status tidak terisi)"
        if not filled.get("note"):
            note += " (catatan tidak terisi)"
        return True, note

    async def _inner_text(self, selector: str) -> str:
        assert self.page is not None
        locator = self.page.locator(selector).first
        try:
            if await locator.count():
                return re.sub(r"\s+", " ", (await locator.inner_text())).strip()
        except PlaywrightError:
            pass
        return ""

    async def capture(self, name: str) -> Path | None:
        if self.page is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60]
        target = self.settings.debug_dir / f"{safe}.png"
        try:
            await self.page.screenshot(path=str(target), full_page=True)
            log.info("screenshot disimpan: %s", target)
            return target
        except Exception as exc:
            log.debug("gagal mengambil screenshot: %s", exc)
            return None

    async def dump_dom(self, name: str) -> Path | None:
        if self.page is None:
            return None
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name)[:60]
        target = self.settings.debug_dir / f"{safe}.html"
        try:
            html = await self.page.content()
            target.write_text(html, encoding="utf-8")
            log.info("DOM disimpan: %s", target)
            return target
        except Exception as exc:
            log.debug("gagal menyimpan DOM: %s", exc)
            return None
