import logging
import os
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from app.models import RecipeData, RecipeStep


LOGGER = logging.getLogger("monsieur_app.uploader")


class LoginError(Exception):
    """Raised when authentication on Monsieur Cuisine fails."""


class UploadError(Exception):
    """Raised when recipe upload automation fails."""


class MonsieurCuisineUploader:
    BASE_URL = "https://www.monsieur-cuisine.com"
    LOGIN_URL = "https://www.monsieur-cuisine.com/it/account/login"
    CREATED_RECIPES_URL = "https://www.monsieur-cuisine.com/it/ricette-create"

    def __init__(self, headless: bool = True) -> None:
        self.headless = headless
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None

    async def __aenter__(self) -> "MonsieurCuisineUploader":
        self._playwright = await async_playwright().start()
        chromium_args = []
        if os.getenv("PLAYWRIGHT_DISABLE_SANDBOX", "1") == "1":
            chromium_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])
        self.browser = await self._playwright.chromium.launch(headless=self.headless, args=chromium_args)
        self.context = await self.browser.new_context(locale="it-IT")
        self.page = await self.context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def wait_visible(self, locator: Locator, timeout: int = 20000) -> None:
        await locator.wait_for(state="visible", timeout=timeout)

    async def fill_first_visible(self, selectors: list[str], value: str) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        for selector in selectors:
            locator = self.page.locator(selector).first
            if await locator.count():
                await self.wait_visible(locator)
                await locator.fill(value)
                return
        raise UploadError(f"Nessun campo trovato per i selettori: {selectors}")

    async def click_first_visible(self, selectors: list[str]) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        for selector in selectors:
            locator = self.page.locator(selector).first
            if await locator.count():
                await self.wait_visible(locator)
                await locator.click()
                return
        raise UploadError(f"Nessun bottone trovato per i selettori: {selectors}")

    async def login_lidl(self, username: str, password: str) -> None:
        if not self.page:
            raise LoginError("Pagina Playwright non inizializzata.")
        try:
            await self.page.goto(self.LOGIN_URL, wait_until="domcontentloaded")

            cookie_accept = self.page.locator(
                "button#onetrust-accept-btn-handler, button:has-text('Accetta'), button:has-text('Accept')"
            ).first
            if await cookie_accept.count():
                await cookie_accept.click()

            await self.fill_first_visible(
                ["input[name='email']", "input[type='email']", "input[autocomplete='username']"],
                username,
            )
            await self.fill_first_visible(
                ["input[name='password']", "input[type='password']", "input[autocomplete='current-password']"],
                password,
            )
            await self.click_first_visible(
                ["button[type='submit']", "button:has-text('Accedi')", "button:has-text('Login')"]
            )
            await self.page.wait_for_load_state("networkidle")

            login_error = self.page.locator("text=/password|credenziali|incorrect|errore|error/i").first
            if await login_error.count():
                raise LoginError("Autenticazione fallita. Verificare username e password.")
        except PlaywrightTimeoutError as exc:
            raise LoginError("Timeout durante il login su Monsieur Cuisine.") from exc
        except PlaywrightError as exc:
            raise LoginError(f"Errore Playwright durante il login: {exc}") from exc

    async def open_created_recipes(self) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            await self.page.goto(self.CREATED_RECIPES_URL, wait_until="domcontentloaded")
            await self.page.wait_for_load_state("networkidle")
            create_button = self.page.locator(
                "a:has-text('Crea ricetta'), button:has-text('Crea ricetta'), a:has-text('Create recipe')"
            ).first
            if await create_button.count():
                await self.wait_visible(create_button)
                await create_button.click()
                await self.page.wait_for_load_state("networkidle")
        except PlaywrightTimeoutError as exc:
            raise UploadError("Timeout durante l'apertura della sezione Ricette Create.") from exc

    async def add_ingredient(self, ingredient: str) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            add_button = self.page.locator(
                "button:has-text('Aggiungi ingrediente'), button:has-text('Add ingredient')"
            ).first
            if await add_button.count():
                await add_button.click()

            ingredient_inputs = self.page.locator(
                "textarea[name*='ingredient'], input[name*='ingredient'], textarea[placeholder*='ingrediente']"
            )
            last_input = ingredient_inputs.last
            await self.wait_visible(last_input)
            await last_input.fill(ingredient)
        except PlaywrightError as exc:
            raise UploadError(f"Errore durante l'inserimento ingrediente '{ingredient}': {exc}") from exc

    async def add_step(self, index: int, step: RecipeStep) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")
        try:
            add_step_button = self.page.locator(
                "button:has-text('Aggiungi step'), button:has-text('Aggiungi fase'), button:has-text('Add step')"
            ).first
            if index > 0 and await add_step_button.count():
                await add_step_button.click()

            step_containers = self.page.locator("[data-testid='recipe-step'], .recipe-step-form, .step-form, fieldset")
            container = step_containers.nth(index) if await step_containers.count() > index else self.page.locator("body")

            description_field = container.locator(
                "textarea[name*='description'], textarea[placeholder*='Descrizione'], textarea"
            ).first
            await self.wait_visible(description_field)
            await description_field.fill(step.description)

            if step.duration_seconds is not None:
                minutes = max(1, round(step.duration_seconds / 60))
                duration_field = container.locator(
                    "input[name*='time'], input[name*='duration'], input[placeholder*='min']"
                ).first
                if await duration_field.count():
                    await duration_field.fill(str(minutes))

            if step.temperature_c is not None:
                temperature_field = container.locator(
                    "input[name*='temp'], input[placeholder*='°'], input[placeholder*='temperatura']"
                ).first
                if await temperature_field.count():
                    await temperature_field.fill(str(step.temperature_c))

            if step.speed is not None:
                speed_field = container.locator(
                    "input[name*='speed'], select[name*='speed'], input[placeholder*='veloc']"
                ).first
                if await speed_field.count():
                    tag_name = await speed_field.evaluate("(node) => node.tagName.toLowerCase()")
                    if tag_name == "select":
                        await speed_field.select_option(label=step.speed)
                    else:
                        await speed_field.fill(step.speed)

            if step.reverse:
                reverse_toggle = container.locator(
                    "input[name*='reverse'], input[type='checkbox'][name*='clock'], label:has-text('Antiorario') input"
                ).first
                if await reverse_toggle.count():
                    await reverse_toggle.check()
        except PlaywrightError as exc:
            raise UploadError(f"Errore durante l'inserimento dello step {index + 1}: {exc}") from exc

    async def upload_to_mc(self, recipe_data: RecipeData) -> None:
        if not self.page:
            raise UploadError("Pagina Playwright non inizializzata.")

        await self.open_created_recipes()
        await self.fill_first_visible(
            ["input[name='title']", "input[placeholder*='Titolo']", "input[placeholder*='title']"],
            recipe_data.title,
        )

        if recipe_data.yield_text:
            try:
                await self.fill_first_visible(
                    ["input[name*='serving']", "input[placeholder*='porzioni']", "input[placeholder*='servings']"],
                    recipe_data.yield_text,
                )
            except UploadError:
                LOGGER.warning("Campo porzioni non trovato, continuo senza questo metadato.")

        if recipe_data.total_time_minutes:
            try:
                await self.fill_first_visible(
                    ["input[name*='totalTime']", "input[name*='duration']", "input[placeholder*='tempo totale']"],
                    str(recipe_data.total_time_minutes),
                )
            except UploadError:
                LOGGER.warning("Campo tempo totale non trovato, continuo senza questo metadato.")

        for ingredient in recipe_data.ingredients:
            await self.add_ingredient(ingredient)

        for index, step in enumerate(recipe_data.steps):
            await self.add_step(index, step)

        submit_button = self.page.locator(
            "button:has-text('Salva'), button:has-text('Pubblica'), button:has-text('Save')"
        ).first
        if await submit_button.count():
            await self.wait_visible(submit_button)
            await submit_button.click()
            await self.page.wait_for_load_state("networkidle")
        else:
            LOGGER.warning("Bottone finale di salvataggio non trovato: verificare i selettori del form.")
