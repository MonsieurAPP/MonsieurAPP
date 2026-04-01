import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from app.models import RecipeData, RecipeStep


LOGGER = logging.getLogger("monsieur_app.uploader")
STATE_BASE_DIR = Path(__file__).resolve().parents[2] / "playwright_state"


class LoginError(Exception):
    """Raised when authentication on Monsieur Cuisine fails."""


class UploadError(Exception):
    """Raised when recipe upload automation fails."""


class AutomationDebugError(Exception):
    """Wraps a domain error with file paths useful for inspection."""

    def __init__(self, message: str, debug_files: Optional[list[str]] = None) -> None:
        super().__init__(message)
        self.debug_files = debug_files or []


class MonsieurCuisineUploader:
    BASE_URL = "https://www.monsieur-cuisine.com"
    LOGIN_URL = "https://www.monsieur-cuisine.com/it/accesso-richiesto"
    CREATED_RECIPES_URL = "https://www.monsieur-cuisine.com/it/ricette-create"
    USER_DATA_DIR = STATE_BASE_DIR / "monsieur_cuisine_browser_profile"
    CDP_PORT = 9222
    CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
    SESSION_MARKER = STATE_BASE_DIR / "session_verified.ok"

    def __init__(self) -> None:
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._owned_page: Optional[Page] = None

    def __enter__(self) -> "MonsieurCuisineUploader":
        if not self.is_browser_available():
            raise LoginError("Browser account non disponibile. Usa prima 'Collega account' e lascia la finestra aperta.")

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.connect_over_cdp(self.CDP_URL)
        if not self._browser.contexts:
            raise LoginError("Impossibile agganciarsi al browser collegato.")

        self.context = self._browser.contexts[0]
        self.page = self.context.new_page()
        self._owned_page = self.page
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned_page:
            try:
                self._owned_page.close()
            except Exception:
                LOGGER.exception("Impossibile chiudere la pagina Playwright")
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                LOGGER.exception("Impossibile chiudere la connessione CDP")
        if self._playwright:
            self._playwright.stop()

    @classmethod
    def browser_candidates(cls) -> list[Path]:
        return [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        ]

    @classmethod
    def find_browser_executable(cls) -> Optional[Path]:
        for candidate in cls.browser_candidates():
            if candidate.exists():
                return candidate
        return None

    @classmethod
    def is_browser_available(cls) -> bool:
        try:
            response = httpx.get(f"{cls.CDP_URL}/json/version", timeout=1.5)
            return response.status_code == 200
        except Exception:
            return False

    @classmethod
    def has_saved_session(cls) -> bool:
        return cls.SESSION_MARKER.exists()

    @classmethod
    def launch_system_login_browser(cls) -> str:
        browser_path = cls.find_browser_executable()
        if not browser_path:
            raise LoginError("Chrome o Edge non trovati sul sistema.")

        cls.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        cls.SESSION_MARKER.parent.mkdir(parents=True, exist_ok=True)
        if cls.SESSION_MARKER.exists():
            cls.SESSION_MARKER.unlink()
        command = [
            str(browser_path),
            f"--remote-debugging-port={cls.CDP_PORT}",
            f"--user-data-dir={cls.USER_DATA_DIR}",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            cls.LOGIN_URL,
        ]

        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

        subprocess.Popen(command, creationflags=creationflags)
        deadline = time.time() + 10
        while time.time() < deadline:
            if cls.is_browser_available():
                return str(browser_path)
            time.sleep(0.5)
        raise LoginError(
            "Il browser si e' aperto ma non e' agganciabile dall'app. Chiudi eventuali finestre Chrome/Edge gia' aperte e riprova."
        )

    @classmethod
    def browser_profile_path(cls) -> str:
        return str(cls.USER_DATA_DIR.resolve())

    @classmethod
    def mark_session_verified(cls) -> None:
        cls.SESSION_MARKER.parent.mkdir(parents=True, exist_ok=True)
        cls.SESSION_MARKER.write_text("verified", encoding="utf-8")

    def wait_visible(self, locator: Locator, timeout: int = 20000) -> None:
        locator.wait_for(state="visible", timeout=timeout)

    def collect_debug_artifacts(self, label: str) -> list[str]:
        if not self.page:
            return []

        debug_dir = Path("debug")
        debug_dir.mkdir(parents=True, exist_ok=True)
        safe_label = "".join(char if char.isalnum() or char in "-_" else "_" for char in label.lower())
        screenshot_path = debug_dir / f"{safe_label}.png"
        html_path = debug_dir / f"{safe_label}.html"

        files: list[str] = []
        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
            files.append(str(screenshot_path.resolve()))
        except Exception:
            LOGGER.exception("Impossibile salvare lo screenshot di debug")

        try:
            html_path.write_text(self.page.content(), encoding="utf-8")
            files.append(str(html_path.resolve()))
        except Exception:
            LOGGER.exception("Impossibile salvare l'HTML di debug")

        return files

    def fill_first_visible(self, selectors: list[str], value: str) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        for selector in selectors:
            locator = self.page.locator(selector).first
            if locator.count():
                self.wait_visible(locator)
                locator.fill(value)
                return
        raise UploadError(f"Nessun campo trovato per i selettori: {selectors}")

    def ensure_logged_in_session(self) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        self.page.goto(self.CREATED_RECIPES_URL, wait_until="domcontentloaded")
        self.page.wait_for_load_state("networkidle")
        body_text = self.page.locator("body").inner_text(timeout=5000).lower()
        if "accesso richiesto" in body_text or "cominciamo ora" in body_text or "log in" in body_text:
            raise LoginError("La sessione del browser non risulta autenticata. Completa il login nella finestra Chrome/Edge dedicata.")

    def open_created_recipes(self) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            self.ensure_logged_in_session()
            self.page.goto(self.CREATED_RECIPES_URL, wait_until="domcontentloaded")
            self.page.wait_for_load_state("networkidle")
            create_button = self.page.locator(
                "a:has-text('Crea ricetta'), button:has-text('Crea ricetta'), a:has-text('Create recipe')"
            ).first
            if create_button.count():
                self.wait_visible(create_button)
                create_button.click()
                self.page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError as exc:
            debug_files = self.collect_debug_artifacts("open_created_recipes_timeout")
            raise AutomationDebugError("Timeout durante l'apertura della sezione Ricette Create.", debug_files) from exc

    def add_ingredient(self, ingredient: str) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            add_button = self.page.locator(
                "button:has-text('Aggiungi ingrediente'), button:has-text('Add ingredient')"
            ).first
            if add_button.count():
                add_button.click()

            ingredient_inputs = self.page.locator(
                "textarea[name*='ingredient'], input[name*='ingredient'], textarea[placeholder*='ingrediente']"
            )
            last_input = ingredient_inputs.last
            self.wait_visible(last_input)
            last_input.fill(ingredient)
        except PlaywrightError as exc:
            debug_files = self.collect_debug_artifacts("ingredient_error")
            raise AutomationDebugError(
                f"Errore durante l'inserimento ingrediente '{ingredient}': {exc}",
                debug_files,
            ) from exc

    def add_step(self, index: int, step: RecipeStep) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            add_step_button = self.page.locator(
                "button:has-text('Aggiungi step'), button:has-text('Aggiungi fase'), button:has-text('Add step')"
            ).first
            if index > 0 and add_step_button.count():
                add_step_button.click()

            step_containers = self.page.locator("[data-testid='recipe-step'], .recipe-step-form, .step-form, fieldset")
            container = step_containers.nth(index) if step_containers.count() > index else self.page.locator("body")

            description_field = container.locator(
                "textarea[name*='description'], textarea[placeholder*='Descrizione'], textarea"
            ).first
            self.wait_visible(description_field)
            description_field.fill(step.description)

            if step.duration_seconds is not None:
                minutes = max(1, round(step.duration_seconds / 60))
                duration_field = container.locator(
                    "input[name*='time'], input[name*='duration'], input[placeholder*='min']"
                ).first
                if duration_field.count():
                    duration_field.fill(str(minutes))

            if step.temperature_c is not None:
                temperature_field = container.locator(
                    "input[name*='temp'], input[placeholder*='°'], input[placeholder*='temperatura']"
                ).first
                if temperature_field.count():
                    temperature_field.fill(str(step.temperature_c))

            if step.speed is not None:
                speed_field = container.locator(
                    "input[name*='speed'], select[name*='speed'], input[placeholder*='veloc']"
                ).first
                if speed_field.count():
                    tag_name = speed_field.evaluate("(node) => node.tagName.toLowerCase()")
                    if tag_name == "select":
                        try:
                            speed_field.select_option(label=step.speed)
                        except PlaywrightError:
                            speed_field.select_option(value=step.speed)
                    else:
                        speed_field.fill(step.speed)

            if step.reverse:
                reverse_toggle = container.locator(
                    "input[name*='reverse'], input[type='checkbox'][name*='clock'], label:has-text('Antiorario') input"
                ).first
                if reverse_toggle.count():
                    reverse_toggle.check()
        except PlaywrightError as exc:
            debug_files = self.collect_debug_artifacts(f"step_{index + 1}_error")
            raise AutomationDebugError(
                f"Errore durante l'inserimento dello step {index + 1}: {exc}",
                debug_files,
            ) from exc

    def upload_to_mc(self, recipe_data: RecipeData) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")

        self.open_created_recipes()
        self.fill_first_visible(
            ["input[name='title']", "input[placeholder*='Titolo']", "input[placeholder*='title']"],
            recipe_data.title,
        )

        if recipe_data.yield_text:
            try:
                self.fill_first_visible(
                    ["input[name*='serving']", "input[placeholder*='porzioni']", "input[placeholder*='servings']"],
                    recipe_data.yield_text,
                )
            except UploadError:
                LOGGER.warning("Campo porzioni non trovato, continuo senza questo metadato.")

        if recipe_data.total_time_minutes:
            try:
                self.fill_first_visible(
                    ["input[name*='totalTime']", "input[name*='duration']", "input[placeholder*='tempo totale']"],
                    str(recipe_data.total_time_minutes),
                )
            except UploadError:
                LOGGER.warning("Campo tempo totale non trovato, continuo senza questo metadato.")

        for ingredient in recipe_data.ingredients:
            self.add_ingredient(ingredient)

        for index, step in enumerate(recipe_data.steps):
            self.add_step(index, step)

        submit_button = self.page.locator(
            "button:has-text('Salva'), button:has-text('Pubblica'), button:has-text('Save')"
        ).first
        if submit_button.count():
            self.wait_visible(submit_button)
            submit_button.click()
            self.page.wait_for_load_state("networkidle")
        else:
            debug_files = self.collect_debug_artifacts("submit_button_missing")
            raise AutomationDebugError(
                "Bottone finale di salvataggio non trovato: verificare i selettori del form.",
                debug_files,
            )


def run_monsieur_cuisine_import(recipe: RecipeData, headless: bool = True) -> None:
    _ = headless
    with MonsieurCuisineUploader() as uploader:
        uploader.ensure_logged_in_session()
        uploader.upload_to_mc(recipe)


def connect_monsieur_cuisine_account(timeout_seconds: int = 180) -> str:
    _ = timeout_seconds
    return MonsieurCuisineUploader.launch_system_login_browser()


def validate_monsieur_cuisine_session() -> None:
    with MonsieurCuisineUploader() as uploader:
        uploader.ensure_logged_in_session()
    MonsieurCuisineUploader.mark_session_verified()
