// ==UserScript==
// @name         Monsieur Cuisine Bridge
// @namespace    https://monsieurapp.local
// @version      0.2.4
// @description  Legge la ricetta confermata da MonsieurAPP e compila il form Monsieur Cuisine nel browser gia' autenticato.
// @match        https://www.monsieur-cuisine.com/*
// @match        https://monsieur-cuisine.com/*
// @match        https://*.monsieur-cuisine.com/*
// @grant        GM_addStyle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  "use strict";

  const APP_BASE_URL = "http://127.0.0.1:8000";
  const TARGET_URL = "https://www.monsieur-cuisine.com/it/create-recipe?devices=mc-smart";

  GM_addStyle(`
    .mc-bridge-fab {
      position: fixed;
      right: 24px;
      bottom: 24px;
      z-index: 2147483647;
      border: 0;
      border-radius: 999px;
      background: linear-gradient(135deg, #f6c445, #f59e0b);
      color: #221b12;
      box-shadow: 0 16px 36px rgba(0, 0, 0, 0.24);
      padding: 14px 18px;
      font: 700 14px/1.2 "Segoe UI", Arial, sans-serif;
      cursor: pointer;
    }
    .mc-bridge-panel {
      position: fixed;
      right: 20px;
      bottom: 20px;
      width: min(360px, calc(100vw - 24px));
      z-index: 2147483647;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 20px;
      background: rgba(23, 23, 23, 0.96);
      color: #fafaf9;
      box-shadow: 0 20px 48px rgba(0, 0, 0, 0.28);
      padding: 16px;
      font: 500 14px/1.5 "Segoe UI", Arial, sans-serif;
    }
    .mc-bridge-panel h3 {
      margin: 0 0 8px;
      font-size: 16px;
      line-height: 1.3;
    }
    .mc-bridge-panel p {
      margin: 0 0 12px;
      color: #d6d3d1;
    }
    .mc-bridge-panel .actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .mc-bridge-panel button {
      border: 0;
      border-radius: 12px;
      padding: 10px 12px;
      cursor: pointer;
      font: 700 13px/1.2 "Segoe UI", Arial, sans-serif;
    }
    .mc-bridge-primary {
      background: #f6c445;
      color: #221b12;
    }
    .mc-bridge-secondary {
      background: rgba(255, 255, 255, 0.08);
      color: #fafaf9;
    }
    .mc-bridge-note {
      margin-top: 10px;
      font-size: 12px;
      color: #a8a29e;
    }
  `);

  function log(...args) {
    console.log("[MC Bridge]", ...args);
  }

  function buildAppUrl(path) {
    return `${APP_BASE_URL}${path}`;
  }

  function apiRequest(method, path) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url: buildAppUrl(path),
        headers: {
          Accept: "application/json",
        },
        onload: (response) => {
          try {
            const parsed = JSON.parse(response.responseText || "{}");
            if (response.status >= 200 && response.status < 300) {
              resolve(parsed);
              return;
            }
            reject(new Error(parsed.detail || `HTTP ${response.status}`));
          } catch (error) {
            reject(new Error(`Risposta API non valida: ${error.message}`));
          }
        },
        onerror: () => reject(new Error("Impossibile raggiungere MonsieurAPP in locale.")),
      });
    });
  }

  function getJobIdFromLocation() {
    const hash = window.location.hash || "";
    const match = hash.match(/mc-import=([0-9a-f-]+)/i);
    return match ? match[1] : null;
  }

  async function resolveConfirmedJob() {
    const explicitJobId = getJobIdFromLocation();
    if (explicitJobId) {
      return { jobId: explicitJobId, source: "hash" };
    }

    const payload = await apiRequest("GET", "/api/imports/latest-confirmed");
    if (!payload?.jobId) {
      throw new Error("Nessuna ricetta confermata disponibile in MonsieurAPP.");
    }

    return { jobId: payload.jobId, source: "latest" };
  }

  function normalizeStepEnvironment(step) {
    const environment = normalizeText(step?.environment || "mc").toLowerCase();
    return environment || "mc";
  }

  function stepHasExplicitEnvironment(step) {
    return Boolean(normalizeText(step?.environment || ""));
  }

  function normalizeStepProgram(step) {
    return normalizeText(step?.program || "");
  }

  function isTransitionLikeStep(step) {
    const program = normalizeStepProgram(step).toLowerCase();
    const signature = buildStepSignature(step).toLowerCase();

    if (program === "prelavaggio") {
      return true;
    }

    return /lavare il boccale|lava il boccale|sciacquare il boccale|risciacquare il boccale|svuotare il boccale|svuota il boccale|pulire il boccale|prelavaggio/.test(signature);
  }

  function shouldUseProgramFallback(rawSteps) {
    if (!rawSteps.length) {
      return false;
    }

    const hasAnyExplicitEnvironment = rawSteps.some((step) => stepHasExplicitEnvironment(step));
    if (hasAnyExplicitEnvironment) {
      return false;
    }

    return rawSteps.some((step) => normalizeStepProgram(step));
  }

  function buildStepSignature(step) {
    return normalizeText(step?.description || step?.detailedInstructions || "");
  }

  function buildIgnoredStepSignatureCounts(payload) {
    const counts = new Map();

    for (const bucket of [payload.manualSteps, payload.transitionSteps]) {
      if (!Array.isArray(bucket)) continue;

      for (const step of bucket) {
        const signature = buildStepSignature(step);
        if (!signature) continue;
        counts.set(signature, (counts.get(signature) || 0) + 1);
      }
    }

    return counts;
  }

  function filterMachineSteps(rawSteps, ignoredSignatureCounts, preferOperationalTimeline, useProgramFallback) {
    let ignoredCount = 0;

    const machineSteps = rawSteps.filter((step) => {
      if (!step || typeof step !== "object") {
        return false;
      }

      if (stepHasExplicitEnvironment(step)) {
        const keepStep = normalizeStepEnvironment(step) === "mc";
        if (!keepStep) {
          ignoredCount += 1;
        }
        return keepStep;
      }

      if (useProgramFallback) {
        const keepStep = Boolean(normalizeStepProgram(step)) && !isTransitionLikeStep(step);
        if (!keepStep) {
          ignoredCount += 1;
        }
        return keepStep;
      }

      if (!preferOperationalTimeline) {
        const signature = buildStepSignature(step);
        const remainingIgnored = signature ? (ignoredSignatureCounts.get(signature) || 0) : 0;
        if (remainingIgnored > 0) {
          ignoredCount += 1;
          if (remainingIgnored === 1) {
            ignoredSignatureCounts.delete(signature);
          } else {
            ignoredSignatureCounts.set(signature, remainingIgnored - 1);
          }
          return false;
        }
      }

      return true;
    });

    return {
      machineSteps,
      ignoredCount,
    };
  }

  function normalizeExportedRecipe(payload) {
    const operationalTimeline = Array.isArray(payload.operationalTimeline)
      ? payload.operationalTimeline
      : [];
    const preferOperationalTimeline = operationalTimeline.length > 0;
    const rawSteps = preferOperationalTimeline
      ? operationalTimeline
      : Array.isArray(payload.steps)
        ? payload.steps
        : [];
    const useProgramFallback = shouldUseProgramFallback(rawSteps);
    const ignoredSignatureCounts = buildIgnoredStepSignatureCounts(payload);
    const { machineSteps, ignoredCount } = filterMachineSteps(rawSteps, ignoredSignatureCounts, preferOperationalTimeline, useProgramFallback);
    const manualStepCount = Array.isArray(payload.manualSteps) ? payload.manualSteps.length : 0;
    const transitionStepCount = Array.isArray(payload.transitionSteps) ? payload.transitionSteps.length : 0;

    return {
      title: payload.title,
      sourceUrl: payload.sourceUrl,
      sourceSite: payload.sourceSite,
      ingredients: Array.isArray(payload.ingredients) ? payload.ingredients : [],
      structuredIngredients: Array.isArray(payload.structuredIngredients)
        ? payload.structuredIngredients.map((ingredient) => ({
            originalText: ingredient.originalText || "",
            name: ingredient.name || "",
            quantity: ingredient.quantity || null,
            unit: ingredient.unit || null,
            notes: ingredient.notes || null,
            scaledQuantity: ingredient.scaledQuantity || null,
            finalQuantity: ingredient.finalQuantity || null,
            finalText: ingredient.finalText || "",
          }))
        : [],
      steps: machineSteps.map((step) => ({
        description: step.description || step.detailedInstructions || "",
        durationSeconds: step.durationSeconds,
        temperatureC: step.temperatureC,
        speed: step.speed,
        reverse: Boolean(step.reverse),
      })),
      ignoredNonMachineStepsCount: preferOperationalTimeline
        ? Math.max(0, rawSteps.length - machineSteps.length)
        : Math.max(ignoredCount, manualStepCount + transitionStepCount),
      yieldText: payload.yieldText || null,
      totalTimeMinutes: payload.totalTimeMinutes || null,
      exportMode: payload.exportMode || "selected",
      selectedReviewMode: payload.selectedReviewMode || "original",
      desiredServings: payload.desiredServings || null,
      warnings: Array.isArray(payload.warnings) ? payload.warnings : [],
    };
  }

  async function fetchRecipeFromApp(jobId) {
    const payload = await apiRequest("GET", `/api/imports/${jobId}/export?mode=selected`);
    const recipe = normalizeExportedRecipe(payload);
    if (recipe.ignoredNonMachineStepsCount) {
      log(`Ignorati ${recipe.ignoredNonMachineStepsCount} passaggi non Monsieur Cuisine dal payload esportato.`);
    }
    return recipe;
  }

  function normalizeText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  const SUPPORTED_INGREDIENT_UNITS = new Set([
    "g",
    "cucchiai",
    "cucchiaini",
    "cucchiaino",
    "cucchiaio",
    "zolla",
    "zolle",
    "pizzico",
    "pizzichi",
  ]);

  function normalizeIngredientUnit(unit) {
    const normalized = normalizeText(unit).toLowerCase();
    if (!normalized) return null;

    const aliases = new Map([
      ["g", "g"],
      ["gr", "g"],
      ["grammo", "g"],
      ["grammi", "g"],
      ["cucchiaio", "cucchiaio"],
      ["cucchiai", "cucchiai"],
      ["cucchiaino", "cucchiaino"],
      ["cucchiaini", "cucchiaini"],
      ["pizzico", "pizzico"],
      ["pizzichi", "pizzichi"],
      ["presa", "pizzico"],
      ["prese", "pizzichi"],
      ["zolla", "zolla"],
      ["zolle", "zolle"],
    ]);

    return aliases.get(normalized) || null;
  }

  function formatIngredientDescription(ingredient) {
    if (typeof ingredient === "string") {
      return normalizeText(ingredient);
    }

    const fallback = normalizeText(ingredient?.finalText || ingredient?.originalText || "");
    const name = normalizeText(ingredient?.name || "");
    const notes = normalizeText(ingredient?.notes || "");
    const originalUnit = normalizeText(ingredient?.unit || "");
    const normalizedUnit = normalizeIngredientUnit(originalUnit);
    const extra = [];

    if (notes) {
      extra.push(notes);
    }
    if (originalUnit && !normalizedUnit) {
      extra.push(originalUnit);
    }

    if (!name) {
      return fallback;
    }
    if (!extra.length) {
      return name;
    }
    return `${name} (${extra.join(", ")})`;
  }

  function normalizeIngredientEntry(ingredient) {
    if (typeof ingredient === "string") {
      return {
        description: normalizeText(ingredient),
        quantity: null,
        unit: null,
      };
    }

    return {
      description: formatIngredientDescription(ingredient),
      quantity: normalizeText(ingredient?.finalQuantity || ingredient?.quantity || "") || null,
      unit: normalizeIngredientUnit(ingredient?.unit),
    };
  }

  function normalizeIngredientQuantity(quantity) {
    const normalized = normalizeText(quantity);
    if (!normalized) return null;

    const aliases = new Map([
      ["un", "1"],
      ["una", "1"],
      ["uno", "1"],
      ["mezzo", "0.5"],
      ["mezza", "0.5"],
    ]);
    if (aliases.has(normalized.toLowerCase())) {
      return aliases.get(normalized.toLowerCase());
    }

    const mixedFractionMatch = normalized.match(/^(\d+)\s+(\d+)\/(\d+)$/);
    if (mixedFractionMatch) {
      const integerPart = Number(mixedFractionMatch[1]);
      const numerator = Number(mixedFractionMatch[2]);
      const denominator = Number(mixedFractionMatch[3]);
      if (denominator) {
        return String(integerPart + (numerator / denominator));
      }
    }

    const simpleFractionMatch = normalized.match(/^(\d+)\/(\d+)$/);
    if (simpleFractionMatch) {
      const numerator = Number(simpleFractionMatch[1]);
      const denominator = Number(simpleFractionMatch[2]);
      if (denominator) {
        return String(numerator / denominator);
      }
    }

    return normalized.replace(/,/g, ".");
  }

  function visibleElementsFromSelectors(selectors) {
    const found = [];
    const seen = new Set();

    for (const selector of selectors) {
      for (const element of visibleElements(selector)) {
        if (!seen.has(element)) {
          seen.add(element);
          found.push(element);
        }
      }
    }

    return found;
  }

  function findIngredientNameInputs() {
    return visibleElementsFromSelectors([
      "input[placeholder*='Ingrediente' i]",
      "input[placeholder*='ingredient' i]",
      "textarea[placeholder*='Ingrediente' i]",
      "textarea[placeholder*='ingredient' i]",
      "textarea[name*='ingredient']",
      "input[name*='ingredient']",
    ]);
  }

  function findIngredientQuantityInputs() {
    return visibleElementsFromSelectors([
      "input[placeholder='0']",
      "input[placeholder='0,0']",
      "input[placeholder='0.0']",
      "input[name*='quantity']",
      "input[name*='amount']",
    ]);
  }

  function findIngredientUnitInputs() {
    return visibleElementsFromSelectors([
      "input[readonly][autocomplete='off']",
      "input[readonly]",
      "input[aria-readonly='false'][readonly]",
    ]);
  }

  function findAlignedRowField(reference, candidates, minLeft = Number.NEGATIVE_INFINITY) {
    if (!reference) return null;

    const referenceRect = reference.getBoundingClientRect();
    const ranked = candidates
      .filter((candidate) => candidate !== reference)
      .map((candidate) => ({
        element: candidate,
        rect: candidate.getBoundingClientRect(),
      }))
      .filter(({ rect }) => rect.left >= minLeft && rect.top <= referenceRect.bottom + 18 && rect.bottom >= referenceRect.top - 18)
      .sort((left, right) => {
        const leftScore = Math.abs(left.rect.top - referenceRect.top) + Math.abs(left.rect.left - referenceRect.right);
        const rightScore = Math.abs(right.rect.top - referenceRect.top) + Math.abs(right.rect.left - referenceRect.right);
        return leftScore - rightScore;
      });

    return ranked[0]?.element || null;
  }

  function findIngredientRowControls(target) {
    const ingredientInputs = findIngredientNameInputs();
    const quantityInputs = findIngredientQuantityInputs();
    const unitInputs = findIngredientUnitInputs();
    const rowIndex = Math.max(0, ingredientInputs.findIndex((element) => element === target));
    const quantityField = findAlignedRowField(target, quantityInputs, target.getBoundingClientRect().right - 8) || quantityInputs[rowIndex] || null;
    const unitReference = quantityField || target;
    const unitField = findAlignedRowField(unitReference, unitInputs, unitReference.getBoundingClientRect().right - 8) || unitInputs[rowIndex] || null;

    return {
      rowIndex,
      ingredientField: target,
      quantityField,
      unitField,
    };
  }

  function getIngredientFieldValue(element) {
    if (!element) return "";

    const slot = element.closest(".v-select__slot, .v-input__slot, .v-input");
    const hiddenInput = slot?.querySelector("input[type='hidden']");
    return normalizeText(hiddenInput?.value || element.value || element.textContent || "");
  }

  function findIngredientFieldNearRow(referenceField, fallbackIndex = 0) {
    const ingredientInputs = findIngredientNameInputs();
    if (!ingredientInputs.length) return null;
    if (!referenceField) return ingredientInputs[fallbackIndex] || ingredientInputs[0] || null;

    const referenceRect = referenceField.getBoundingClientRect();
    const ranked = ingredientInputs
      .map((element) => ({
        element,
        rect: element.getBoundingClientRect(),
        value: getIngredientFieldValue(element),
      }))
      .sort((left, right) => {
        const leftScore = Math.abs(left.rect.top - referenceRect.top) + Math.abs(left.rect.left - referenceRect.left);
        const rightScore = Math.abs(right.rect.top - referenceRect.top) + Math.abs(right.rect.left - referenceRect.left);
        return leftScore - rightScore;
      });

    return ranked[0]?.element || ingredientInputs[fallbackIndex] || ingredientInputs[0] || null;
  }

  function getIngredientRowControlsByIndex(rowIndex) {
    const ingredientField = findIngredientNameInputs()[rowIndex] || null;
    if (!ingredientField) {
      return {
        rowIndex,
        ingredientField: null,
        quantityField: findIngredientQuantityInputs()[rowIndex] || null,
        unitField: findIngredientUnitInputs()[rowIndex] || null,
      };
    }
    return findIngredientRowControls(ingredientField);
  }

  async function setIngredientQuantityField(quantityField, quantityValue) {
    const normalizedQuantity = normalizeIngredientQuantity(quantityValue);
    if (!quantityField) {
      return false;
    }

    if (!normalizedQuantity) {
      quantityField.scrollIntoView?.({ block: "center", inline: "center" });
      await commitTextInputLikeUser(quantityField, "");
      return normalizeText(quantityField.value) === "";
    }

    for (let attempt = 0; attempt < 3; attempt += 1) {
      await commitTextInputLikeUser(quantityField, normalizedQuantity);
      if (normalizeText(quantityField.value) === normalizedQuantity) {
        return true;
      }
    }

    return false;
  }

  async function settleIngredientPage() {
    for (const input of findIngredientQuantityInputs()) {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.blur?.();
      input.dispatchEvent(new Event("blur", { bubbles: true }));
    }

    for (const input of findIngredientNameInputs()) {
      input.dispatchEvent(new Event("change", { bubbles: true }));
      input.blur?.();
      input.dispatchEvent(new Event("blur", { bubbles: true }));
    }

    document.body?.click?.();
    await sleep(600);
  }

  async function selectIngredientUnit(unitField, unitValue) {
    if (!unitField || !unitValue || !SUPPORTED_INGREDIENT_UNITS.has(unitValue)) {
      return false;
    }

    const trigger = unitField.closest("[role='combobox'], .v-select, .v-autocomplete, .v-input, .v-input__slot") || unitField;
    clickElementRobust(trigger);

    const deadline = Date.now() + 4000;
    while (Date.now() < deadline) {
      const option = visibleElements("[role='option'], .v-list-item")
        .find((element) => normalizeText(element.textContent).toLowerCase() === unitValue.toLowerCase());
      if (option) {
        clickElementRobust(option);
        return true;
      }
      await sleep(150);
    }

    return false;
  }


  function createFab(label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "mc-bridge-fab";
    button.textContent = label;
    button.addEventListener("click", onClick);
    document.body.appendChild(button);
    return button;
  }

  function createPanel({ title, bodyHtml, primaryLabel, onPrimary, secondaryLabel, onSecondary, note }) {
    const panel = document.createElement("div");
    panel.className = "mc-bridge-panel";
    const actions = [];
    if (primaryLabel) {
      actions.push(`<button type="button" class="mc-bridge-primary">${escapeHtml(primaryLabel)}</button>`);
    }
    if (secondaryLabel) {
      actions.push(`<button type="button" class="mc-bridge-secondary">${escapeHtml(secondaryLabel)}</button>`);
    }
    panel.innerHTML = `
      <h3>${escapeHtml(title)}</h3>
      <p>${bodyHtml}</p>
      ${actions.length ? `<div class="actions">${actions.join("")}</div>` : ""}
      <div class="mc-bridge-note">${escapeHtml(note || "Lo script non preme Salva: controlla il form e conferma tu.")}</div>
    `;

    const buttons = panel.querySelectorAll("button");
    if (primaryLabel && buttons[0] && onPrimary) {
      buttons[0].addEventListener("click", onPrimary);
    }
    if (secondaryLabel && buttons[primaryLabel ? 1 : 0] && onSecondary) {
      buttons[primaryLabel ? 1 : 0].addEventListener("click", onSecondary);
    }
    document.body.appendChild(panel);
    return panel;
  }

  function createRecipePanel(recipe, onApply, onDismiss) {
    const ignoredStepsNote = recipe.ignoredNonMachineStepsCount
      ? `<br>${recipe.ignoredNonMachineStepsCount} passaggi esterni/manuali ignorati automaticamente`
      : "";

    return createPanel({
      title: "Ricetta confermata pronta",
      bodyHtml: `<strong>${escapeHtml(recipe.title)}</strong><br>${recipe.ingredients.length} ingredienti, ${recipe.steps.length} step Monsieur Cuisine${ignoredStepsNote}`,
      primaryLabel: "Leggi ricetta confermata",
      onPrimary: onApply,
      secondaryLabel: "Chiudi",
      onSecondary: onDismiss,
      note: "Lo script non preme Salva: controlla il form e conferma tu.",
    });
  }

  function createUnavailablePanel(message, onRetry, onDismiss) {
    return createPanel({
      title: "Nessuna ricetta pronta",
      bodyHtml: `${escapeHtml(message)}<br><br>Conferma prima una ricetta in MonsieurAPP, poi torna qui e premi Riprova.`,
      primaryLabel: "Riprova",
      onPrimary: onRetry,
      secondaryLabel: "Chiudi",
      onSecondary: onDismiss,
      note: "Se apri Monsieur Cuisine dalla review page confermata, lo script usa direttamente il job indicato nell'hash.",
    });
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function truncateText(value, maxLength) {
    const normalized = normalizeText(value);
    if (!normalized || normalized.length <= maxLength) return normalized;
    return `${normalized.slice(0, Math.max(0, maxLength - 1)).trim()}…`;
  }

  function buildShortDescription(recipe) {
    const firstStep = truncateText(recipe.steps?.[0]?.description || "", 180);
    if (firstStep) return firstStep;

    const fallback = `${recipe.title}. Ricetta importata da ${recipe.sourceSite || "sito sorgente"}.`;
    return truncateText(fallback, 180);
  }

  function minutesToHourMinute(totalMinutes) {
    const safeMinutes = Math.max(0, Number(totalMinutes || 0));
    return {
      hours: String(Math.floor(safeMinutes / 60)),
      minutes: String(safeMinutes % 60),
    };
  }

  const EDITABLE_FIELD_SELECTOR = [
    "input:not([type='hidden']):not([type='checkbox']):not([type='radio'])",
    "textarea",
    "select",
    "[contenteditable='true']",
    "[role='textbox']",
  ].join(", ");

  function setNativeValue(element, value) {
    if (!element) return false;
    const normalizedValue = String(value ?? "");

    if (element.isContentEditable || element.getAttribute("contenteditable") === "true" || element.getAttribute("role") === "textbox") {
      const previous = element.textContent || "";
      element.focus();
      element.textContent = normalizedValue;
      if (previous !== normalizedValue) {
        element.dispatchEvent(new InputEvent("input", { bubbles: true, data: normalizedValue, inputType: "insertText" }));
        element.dispatchEvent(new Event("change", { bubbles: true }));
        element.dispatchEvent(new Event("blur", { bubbles: true }));
      }
      return true;
    }

    const previous = element.value;
    const prototype = Object.getPrototypeOf(element);
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor?.set) {
      descriptor.set.call(element, normalizedValue);
    } else {
      element.value = normalizedValue;
    }
    if (previous !== normalizedValue) {
      element.dispatchEvent(new Event("input", { bubbles: true }));
      element.dispatchEvent(new Event("change", { bubbles: true }));
      element.dispatchEvent(new Event("blur", { bubbles: true }));
    }
    return true;
  }

  async function commitTextInputLikeUser(element, value) {
    if (!element) return false;

    const normalizedValue = String(value ?? "");
    element.scrollIntoView?.({ block: "center", inline: "center" });
    element.click?.();
    await sleep(60);
    element.focus?.();
    setNativeValue(element, normalizedValue);

    element.dispatchEvent(new InputEvent("beforeinput", {
      bubbles: true,
      cancelable: true,
      composed: true,
      data: normalizedValue,
      inputType: "insertText",
    }));
    element.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      cancelable: true,
      composed: true,
      data: normalizedValue,
      inputType: "insertText",
    }));

    for (const character of normalizedValue) {
      const keyCode = character === "." ? "Period" : `Digit${character}`;
      element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: character, code: keyCode }));
      element.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: character, code: keyCode }));
    }

    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Tab", code: "Tab" }));
    element.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: "Tab", code: "Tab" }));
    element.blur?.();
    element.dispatchEvent(new Event("blur", { bubbles: true }));
    await sleep(120);
    return true;
  }

  function visibleElements(selector) {
    return Array.from(document.querySelectorAll(selector)).filter((element) => {
      const style = window.getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
    });
  }

  function collectFieldText(element) {
    if (!element) return "";

    const chunks = [
      element.getAttribute("name"),
      element.getAttribute("placeholder"),
      element.getAttribute("aria-label"),
      element.getAttribute("data-testid"),
      element.id,
      element.textContent,
    ];

    const labelledBy = element.getAttribute("aria-labelledby");
    if (labelledBy) {
      labelledBy.split(/\s+/).filter(Boolean).forEach((id) => {
        const node = document.getElementById(id);
        if (node) chunks.push(node.textContent || "");
      });
    }

    if (element.id && window.CSS?.escape) {
      const labels = document.querySelectorAll(`label[for="${window.CSS.escape(element.id)}"]`);
      labels.forEach((label) => chunks.push(label.textContent || ""));
    }

    let current = element;
    for (let depth = 0; current && depth < 4; depth += 1) {
      chunks.push(current.getAttribute?.("data-testid"));
      chunks.push(current.getAttribute?.("aria-label"));
      chunks.push(current.textContent || "");
      current = current.parentElement;
    }

    return normalizeText(chunks.filter(Boolean).join(" ")).toLowerCase();
  }

  function findField({ selectors = [], patterns = [] } = {}) {
    for (const selector of selectors) {
      const target = visibleElements(selector)[0];
      if (target) return target;
    }

    const normalizedPatterns = patterns.map((pattern) => normalizeText(pattern).toLowerCase()).filter(Boolean);
    if (!normalizedPatterns.length) return null;

    const candidates = visibleElements(EDITABLE_FIELD_SELECTOR)
      .map((element) => {
        const haystack = collectFieldText(element);
        const score = normalizedPatterns.reduce((total, pattern) => total + (haystack.includes(pattern) ? 1 : 0), 0);
        return { element, haystack, score };
      })
      .filter((candidate) => candidate.score > 0)
      .sort((left, right) => right.score - left.score);

    return candidates[0]?.element || null;
  }

  function findAllFields({ selectors = [], patterns = [] } = {}) {
    const found = [];
    const seen = new Set();

    for (const selector of selectors) {
      for (const element of visibleElements(selector)) {
        if (!seen.has(element)) {
          seen.add(element);
          found.push(element);
        }
      }
    }

    const normalizedPatterns = patterns.map((pattern) => normalizeText(pattern).toLowerCase()).filter(Boolean);
    if (!normalizedPatterns.length) return found;

    for (const element of visibleElements(EDITABLE_FIELD_SELECTOR)) {
      if (seen.has(element)) continue;
      const haystack = collectFieldText(element);
      if (normalizedPatterns.some((pattern) => haystack.includes(pattern))) {
        seen.add(element);
        found.push(element);
      }
    }

    return found;
  }

  function debugVisibleFields(limit = 8) {
    return visibleElements(EDITABLE_FIELD_SELECTOR)
      .slice(0, limit)
      .map((element, index) => `${index + 1}. ${element.tagName.toLowerCase()} -> ${collectFieldText(element).slice(0, 120) || "senza etichette"}`)
      .join("\n");
  }

  function debugVisibleButtons(limit = 12) {
    return visibleElements("button, a, [role='button']")
      .filter((element) => !isElementDisabled(element))
      .slice(0, limit)
      .map((element, index) => {
        const text = normalizeText([
          element.textContent,
          element.getAttribute("aria-label"),
          element.getAttribute("title"),
          element.getAttribute("data-testid"),
          element.className,
        ].filter(Boolean).join(" "));
        return `${index + 1}. ${element.tagName.toLowerCase()} -> ${text || "senza etichette"}`;
      })
      .join("\n");
  }

  function isElementDisabled(element) {
    return Boolean(element?.disabled || element?.getAttribute("aria-disabled") === "true");
  }

  function collectButtonText(element) {
    if (!element) return "";

    return normalizeText([
      element.textContent,
      element.getAttribute?.("aria-label"),
      element.getAttribute?.("title"),
      element.getAttribute?.("data-testid"),
      element.className,
      element.querySelector?.("img")?.getAttribute?.("src"),
      element.querySelector?.("i")?.className,
    ].filter(Boolean).join(" ")).toLowerCase();
  }

  function isVisibleElement(element) {
    if (!(element instanceof Element)) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== "hidden" && style.display !== "none" && rect.width > 0 && rect.height > 0;
  }

  function isInsideVisibleDialog(element) {
    const dialog = element?.closest?.(".v-dialog, .v-dialog__content, .v-overlay__content, .menuable__content__active, [role='dialog']");
    return isVisibleElement(dialog);
  }

  function clickFirst(selectors) {
    for (const selector of selectors) {
      const target = visibleElements(selector)[0];
      if (target) {
        target.click();
        return true;
      }
    }
    return false;
  }

  function fillFirst(selectors, value) {
    if (!normalizeText(value)) return false;
    const target = findField({ selectors });
    if (target) {
      target.focus();
      return setNativeValue(target, value);
    }
    return false;
  }

  function getVisibleTimeInputs() {
    return visibleElements("input:not([type='hidden']):not([type='checkbox']):not([type='radio'])")
      .filter((element) => {
        const inputType = (element.getAttribute("type") || "text").toLowerCase();
        return ["text", "number", "tel", "search", ""].includes(inputType);
      });
  }

  function fillTimeWizardPage(recipe) {
    const visibleInputs = getVisibleTimeInputs();
    if (visibleInputs.length < 4) {
      throw new Error(`Campi tempo non trovati o incompleti. Campi visibili:\n${debugVisibleFields()}`);
    }

    const prepTime = minutesToHourMinute(recipe.totalTimeMinutes || 0);
    const totalTime = minutesToHourMinute(recipe.totalTimeMinutes || 0);

    setNativeValue(visibleInputs[0], prepTime.hours);
    setNativeValue(visibleInputs[1], prepTime.minutes);
    setNativeValue(visibleInputs[2], totalTime.hours);
    setNativeValue(visibleInputs[3], totalTime.minutes);
  }

  async function waitForField({ selectors = [], patterns = [] } = {}, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const target = findField({ selectors, patterns });
      if (target) return target;
      await sleep(250);
    }
    return null;
  }

  async function waitForEnabledButtonByText(pattern, timeoutMs = 5000, options = {}) {
    const excludePatterns = Array.isArray(options.excludePatterns) ? options.excludePatterns : [];
    const preferDialog = options.preferDialog !== false;
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const target = uniqueElements(
        visibleElements("button, a, [role='button'], [tabindex], .v-btn, .btn")
          .map((element) => element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element)
      )
        .filter((element) => !isElementDisabled(element))
        .map((element) => ({
          element,
          text: collectButtonText(element),
          inDialog: isInsideVisibleDialog(element),
        }))
        .filter((candidate) => pattern.test(candidate.text))
        .filter((candidate) => !excludePatterns.some((excludePattern) => excludePattern.test(candidate.text)))
        .sort((left, right) => {
          if (preferDialog && left.inDialog !== right.inDialog) {
            return Number(right.inDialog) - Number(left.inDialog);
          }
          return left.text.length - right.text.length;
        })[0]?.element;
      if (target) return target;
      await sleep(150);
    }
    return null;
  }

  function readFieldValue(element) {
    if (!element) return "";
    if (element.isContentEditable || element.getAttribute("contenteditable") === "true" || element.getAttribute("role") === "textbox") {
      return normalizeText(element.textContent || "");
    }
    return normalizeText(element.value ?? element.textContent ?? "");
  }

  async function ensureFieldValue(element, value, label) {
    const expected = normalizeText(value);
    const actual = readFieldValue(element);
    if (actual === expected) return true;

    await commitTextInputLikeUser(element, expected);
    await sleep(120);
    const retried = readFieldValue(element);
    if (retried === expected) return true;

    throw new Error(`${label} step non valorizzato correttamente. Atteso: '${expected}'. Letto: '${retried || "vuoto"}'.`);
  }

  async function pressStepSubmitButton(button) {
    if (!button) return false;

    button.scrollIntoView?.({ block: "center", inline: "center" });
    button.focus?.();
    clickElementRobust(button);
    await sleep(250);

    if (isElementDisabled(button)) {
      return false;
    }

    for (const currentTarget of [button, button.querySelector?.("span"), button.parentElement].filter(Boolean)) {
      try {
        currentTarget.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
      } catch (error) {
        log("Submit click dispatch fallito", error);
      }
      try {
        currentTarget.click?.();
      } catch (error) {
        log("Submit click() fallito", error);
      }
      try {
        currentTarget.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter", code: "Enter" }));
        currentTarget.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: "Enter", code: "Enter" }));
      } catch (error) {
        log("Submit keyboard dispatch fallito", error);
      }
    }

    return true;
  }

  async function waitForNewFieldCount(fieldConfig, previousCount, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const fields = findAllFields(fieldConfig);
      if (fields.length > previousCount) return fields;
      await sleep(250);
    }
    return findAllFields(fieldConfig);
  }

  async function fillIngredient(ingredient, index) {
    const fieldConfig = {
      selectors: [
        "textarea[name*='ingredient']",
        "input[name*='ingredient']",
        "textarea[placeholder*='ingrediente' i]",
        "input[placeholder*='ingrediente' i]",
      ],
      patterns: ["ingrediente", "ingredient"],
    };

    const existingFields = findAllFields(fieldConfig);
    if (!await tryAddIngredientButton(fieldConfig, existingFields.length)) {
      throw new Error(`Bottone per aggiungere un ingrediente non trovato. Bottoni visibili:\n${debugVisibleButtons()}\n\nCampi visibili:\n${debugVisibleFields()}`);
    }

    const fields = await waitForNewFieldCount(fieldConfig, existingFields.length, 15000);
    const target = fields[fields.length - 1] || await waitForField(fieldConfig);
    if (!target) {
      throw new Error(`Campo ingrediente non trovato per la riga ${index + 1}.`);
    }

    const normalizedIngredient = normalizeIngredientEntry(ingredient);
    const rowControls = findIngredientRowControls(target);

    rowControls.ingredientField.scrollIntoView?.({ block: "center", inline: "center" });
    rowControls.ingredientField.focus();
    setNativeValue(rowControls.ingredientField, normalizedIngredient.description);
    await sleep(120);

    if (rowControls.unitField && normalizedIngredient.unit) {
      await selectIngredientUnit(rowControls.unitField, normalizedIngredient.unit);
      await sleep(150);
    }

    const refreshedIngredientField = findIngredientFieldNearRow(rowControls.ingredientField, rowControls.rowIndex);
    const refreshedRowControls = refreshedIngredientField
      ? findIngredientRowControls(refreshedIngredientField)
      : getIngredientRowControlsByIndex(rowControls.rowIndex);
    if (refreshedRowControls.quantityField) {
      refreshedRowControls.quantityField.scrollIntoView?.({ block: "center", inline: "center" });
      await setIngredientQuantityField(refreshedRowControls.quantityField, normalizedIngredient.quantity);
    }

    document.body?.click?.();
  }

  function buildStepTitle(step, index) {
    const source = normalizeText(step?.description || "");
    const sentence = source.split(/[.!?]/)[0]?.trim();
    const compact = truncateText(sentence || source, 60);
    return compact || `Passaggio ${index + 1}`;
  }

  function stepListPageMarkerConfig() {
    return {
      selectors: [
        "button:has(img[src*='glyphs-btn-step'])",
        "button:has(i.mdi.mdi-plus)",
        "img[src*='glyphs-btn-step']",
        ".create-recipe-step-list",
        ".create-recipe-step-group-content",
      ],
      patterns: [
        "inserisci il tuo primo passaggio",
        "primo passaggio",
        "passaggi",
        "passaggio",
        "step",
      ],
    };
  }

  function stepEditorFieldConfig() {
    return {
      selectors: [
        "input[name*='title']",
        "input[placeholder*='titolo' i]",
        "input[placeholder*='title' i]",
        "input[placeholder*='Preparare le cipolle' i]",
        "textarea[name*='description']",
        "textarea[placeholder*='descrizione' i]",
        "textarea[placeholder*='description' i]",
        "textarea[placeholder*='Sbucciare le cipolle' i]",
        "[role='textbox']",
      ],
      patterns: ["titolo", "title", "descrizione", "description", "passaggio", "step", "fase"],
    };
  }

  function collectStepButtonText(element) {
    if (!element) return "";

    return normalizeText([
      element.textContent,
      element.getAttribute?.("aria-label"),
      element.getAttribute?.("title"),
      element.getAttribute?.("data-testid"),
      element.className,
      element.querySelector?.("img")?.getAttribute("src"),
      element.querySelector?.("i")?.className,
    ].filter(Boolean).join(" ")).toLowerCase();
  }

  function findVisibleStepCards() {
    return visibleElementsFromSelectors([
      "[data-testid='recipe-step']",
      ".create-recipe-step-group-content .v-card",
      ".create-recipe-step-list .v-card",
      ".create-recipe-step-group-content [class*='step-card' i]",
      ".create-recipe-step-list [class*='step-card' i]",
    ]).filter((element) => {
      const text = collectStepButtonText(element);
      if (!text) return false;

      return !/inserisci il tuo primo passaggio|inserisci il primo passaggio|aggiungi step|aggiungi fase|add step|new step/.test(text);
    });
  }

  function sortAddStepButtons(candidates, index) {
    const stepCards = findVisibleStepCards().sort((left, right) => {
      return right.getBoundingClientRect().bottom - left.getBoundingClientRect().bottom;
    });
    const lastStepCardBottom = stepCards[0]?.getBoundingClientRect().bottom ?? Number.NEGATIVE_INFINITY;

    return uniqueElements(candidates)
      .filter((element) => element instanceof HTMLElement && !isElementDisabled(element))
      .map((element) => {
        const rect = element.getBoundingClientRect();
        const text = collectStepButtonText(element);
        const isFirstStepTarget = /inserisci il tuo primo passaggio|inserisci il primo passaggio|first step/.test(text);
        const isAddStepTarget = /aggiungi step|aggiungi fase|add step|new step|mdi-plus|glyphs-btn-step|btn-step|plus/.test(text);
        const isBelowLastCard = rect.top >= lastStepCardBottom - 24;
        let score = rect.top * 1000 + rect.left;

        if (index === 0 && isFirstStepTarget) score += 1000000;
        if (index > 0 && isAddStepTarget) score += 500000;
        if (index > 0 && isBelowLastCard) score += 750000;
        if (index > 0 && isFirstStepTarget) score -= 1000000;

        return {
          element,
          rect,
          score,
        };
      })
      .sort((left, right) => right.score - left.score || right.rect.top - left.rect.top || right.rect.left - left.rect.left)
      .map((candidate) => candidate.element);
  }

  function clickSortedAddStepButtonByText(pattern, index) {
    const candidates = Array.from(document.querySelectorAll("button, a, [role='button']"))
      .filter((element) => !isElementDisabled(element) && pattern.test(normalizeText(element.textContent)));
    const target = sortAddStepButtons(candidates, index)[0];
    if (!target) {
      return false;
    }

    clickElementRobust(target);
    return true;
  }

  function candidateAddStepButtons() {
    const candidates = [];

    const firstStepCards = Array.from(document.querySelectorAll("div[tabindex], .v-card, [role='button']"))
      .filter((element) => /inserisci il tuo primo passaggio|inserisci il primo passaggio|first step/i.test(normalizeText(element.textContent)));
    candidates.push(...firstStepCards.map((element) => {
      return element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn, .v-card") || element;
    }));

    candidates.push(...Array.from(document.querySelectorAll("img[src*='glyphs-btn-step'], img[src*='btn-step']")).map((element) => {
      return element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
    }));

    candidates.push(...Array.from(document.querySelectorAll("i.mdi.mdi-plus, .mdi-plus")).map((element) => {
      return element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
    }));

    const passaggiAnchors = Array.from(document.querySelectorAll("div, span, p, h1, h2, h3, h4, h5, h6, button"))
      .filter((element) => /passaggi|passaggio|step/i.test(normalizeText(element.textContent)));

    for (const anchor of passaggiAnchors) {
      const anchorRect = anchor.getBoundingClientRect();
      const nearby = visibleElements("button, a, [role='button'], [tabindex], .v-btn, .btn, i, img")
        .map((element) => {
          const clickable = element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
          const rect = clickable.getBoundingClientRect();
          const text = normalizeText([
            clickable.textContent,
            clickable.getAttribute?.("aria-label"),
            clickable.getAttribute?.("title"),
            clickable.className,
            element.getAttribute?.("src"),
            element.className,
          ].filter(Boolean).join(" ")).toLowerCase();
          const aligned = rect.top <= anchorRect.bottom + 48 && rect.bottom >= anchorRect.top - 48;
          const rightSide = rect.left >= anchorRect.right - 24;
          return { clickable, aligned, rightSide, text };
        })
        .filter((item) => item.aligned && item.rightSide)
        .sort((left, right) => right.clickable.getBoundingClientRect().left - left.clickable.getBoundingClientRect().left);

      candidates.push(...nearby.map((item) => item.clickable));
    }

    const textTargets = Array.from(document.querySelectorAll("button, a, [role='button']"))
      .filter((element) => /inserisci il tuo primo passaggio|inserisci il primo passaggio|aggiungi step|aggiungi fase|add step|new step/i.test(normalizeText(element.textContent)));
    candidates.push(...textTargets);

    return uniqueElements(candidates).filter((element) => element instanceof HTMLElement && !isElementDisabled(element));
  }

  async function openStepEditor(index) {
    const candidates = sortAddStepButtons(candidateAddStepButtons(), index);
    for (const candidate of candidates) {
      clickElementRobust(candidate);
      await sleep(700);
      const editorField = await waitForField(stepEditorFieldConfig(), 2500);
      if (editorField) {
        return true;
      }
    }

    if (index === 0 && clickByText(/inserisci il tuo primo passaggio|inserisci il primo passaggio|first step/i)) {
      await sleep(700);
      return Boolean(await waitForField(stepEditorFieldConfig(), 2500));
    }

    if (index > 0 && clickSortedAddStepButtonByText(/aggiungi step|aggiungi fase|add step|new step/i, index)) {
      await sleep(700);
      return Boolean(await waitForField(stepEditorFieldConfig(), 2500));
    }

    return false;
  }

  async function saveStepEditor() {
    const submitButton = await waitForEnabledButtonByText(/inserisci|inserire|salvare|salva|save/i, 5000, {
      preferDialog: true,
      excludePatterns: [
        /inserisci il tuo primo passaggio|inserisci il primo passaggio/i,
        /inserire passaggio|aggiungi step|aggiungi fase|add step|new step/i,
      ],
    });
    if (!submitButton) {
      throw new Error(`Bottone salva step non trovato. Bottoni visibili:\n${debugVisibleButtons()}`);
    }

    if (isElementDisabled(submitButton)) {
      throw new Error(`Bottone Inserire ancora disabilitato. Bottoni visibili:\n${debugVisibleButtons()}`);
    }

    await pressStepSubmitButton(submitButton);

    await sleep(1200);
    const backOnList = await waitForField(stepListPageMarkerConfig(), 8000);
    if (!backOnList) {
      throw new Error(`Il salvataggio dello step non riporta alla lista passaggi. Campi visibili:\n${debugVisibleFields()}`);
    }
    return true;
  }

  async function fillStep(step, index) {
    const opened = await openStepEditor(index);
    if (!opened) {
      throw new Error(`Editor step non trovato per lo step ${index + 1}. Bottoni visibili:\n${debugVisibleButtons()}`);
    }

    const titleField = findField({
      selectors: [
        "input[name*='title']",
        "input[placeholder*='titolo' i]",
        "input[placeholder*='title' i]",
        "input[placeholder*='Preparare le cipolle' i]",
      ],
      patterns: ["titolo", "title", "nome step", "step title", "passaggio", "preparare le cipolle"],
    });
    if (titleField) {
      titleField.scrollIntoView?.({ block: "center", inline: "center" });
      const stepTitle = buildStepTitle(step, index);
      await commitTextInputLikeUser(titleField, stepTitle);
      await ensureFieldValue(titleField, stepTitle, "Titolo");
      await sleep(120);
    }

    const descriptionField = await waitForField({
      selectors: [
        "textarea[name*='description']",
        "textarea[placeholder*='descrizione' i]",
        "textarea[placeholder*='description' i]",
        "textarea[placeholder*='Sbucciare le cipolle' i]",
        "textarea",
        "[role='textbox']",
        "[contenteditable='true']",
      ],
      patterns: ["descrizione", "description", "istruzioni", "passaggio", "step", "fase", "sbucciare le cipolle"],
    }, 5000);
    if (!descriptionField) {
      throw new Error(`Campo descrizione step non trovato per lo step ${index + 1}.`);
    }
    descriptionField.scrollIntoView?.({ block: "center", inline: "center" });
    await commitTextInputLikeUser(descriptionField, step.description);
    await ensureFieldValue(descriptionField, step.description, "Descrizione");
    await sleep(200);

    const minutes = step.durationSeconds ? String(Math.max(1, Math.round(step.durationSeconds / 60))) : null;
    if (minutes) {
      const durationField = findField({
        selectors: [
          "input[name*='time']",
          "input[name*='duration']",
          "input[placeholder*='min' i]",
        ],
        patterns: ["min", "tempo", "duration", "durata"],
      });
      if (durationField) {
        durationField.focus();
        setNativeValue(durationField, minutes);
      }
    }

    if (step.temperatureC) {
      const temperatureField = findField({
        selectors: [
          "input[name*='temp']",
          "input[placeholder*='temperatura' i]",
          "input[placeholder*='°' i]",
        ],
        patterns: ["temperatura", "temp", "°"],
      });
      if (temperatureField) {
        temperatureField.focus();
        setNativeValue(temperatureField, String(step.temperatureC));
      }
    }

    if (step.speed) {
      const speedField = findField({
        selectors: [
          "input[name*='speed']",
          "select[name*='speed']",
          "input[placeholder*='veloc' i]",
        ],
        patterns: ["veloc", "speed"],
      });
      if (speedField) {
        speedField.focus?.();
        setNativeValue(speedField, String(step.speed));
      }
    }

    await saveStepEditor();
  }

  function fillNth(selectors, index, value) {
    for (const selector of selectors) {
      const items = visibleElements(selector);
      const target = items[index];
      if (target) {
        target.focus();
        return setNativeValue(target, value);
      }
    }
    return false;
  }

  function clickByText(pattern) {
    const candidates = Array.from(document.querySelectorAll("button, a, [role='button']"));
    const target = candidates.find((element) => !isElementDisabled(element) && pattern.test(normalizeText(element.textContent)));
    if (target) {
      clickElementRobust(target);
      return true;
    }
    return false;
  }

  function clickElementRobust(element) {
    if (!element) return false;

    const target = element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
    target.scrollIntoView({ block: "center", inline: "center" });
    const rect = target.getBoundingClientRect();
    const clientX = rect.left + Math.max(1, rect.width) / 2;
    const clientY = rect.top + Math.max(1, rect.height) / 2;
    const hitTarget = document.elementFromPoint(clientX, clientY);
    const interactionTargets = uniqueElements([
      target,
      hitTarget?.closest?.("button, a, [role='button'], [tabindex], .v-btn, .btn") || hitTarget,
    ]);

    target.focus?.();
    const mouseOptions = {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX,
      clientY,
      screenX: window.screenX + clientX,
      screenY: window.screenY + clientY,
      button: 0,
      buttons: 1,
      detail: 1,
    };
    const touchSupported = typeof TouchEvent === "function";
    const touchOptions = {
      bubbles: true,
      cancelable: true,
      composed: true,
      touches: [],
      targetTouches: [],
      changedTouches: [],
    };

    for (const currentTarget of interactionTargets) {
      currentTarget.focus?.();
      for (const [eventName, eventFactory] of [
        ["pointerover", () => new PointerEvent("pointerover", { ...mouseOptions, pointerType: "mouse", isPrimary: true, buttons: 0 })],
        ["mouseover", () => new MouseEvent("mouseover", { ...mouseOptions, buttons: 0 })],
        ["pointerenter", () => new PointerEvent("pointerenter", { ...mouseOptions, pointerType: "mouse", isPrimary: true, buttons: 0 })],
        ["mouseenter", () => new MouseEvent("mouseenter", { ...mouseOptions, bubbles: false, buttons: 0 })],
        ["touchstart", () => touchSupported ? new TouchEvent("touchstart", touchOptions) : new Event("touchstart", { bubbles: true, cancelable: true })],
        ["pointerdown", () => new PointerEvent("pointerdown", { ...mouseOptions, pointerType: "mouse", isPrimary: true, pressure: 0.5 })],
        ["mousedown", () => new MouseEvent("mousedown", mouseOptions)],
        ["touchend", () => touchSupported ? new TouchEvent("touchend", touchOptions) : new Event("touchend", { bubbles: true, cancelable: true })],
        ["pointerup", () => new PointerEvent("pointerup", { ...mouseOptions, pointerType: "mouse", isPrimary: true, pressure: 0, buttons: 0 })],
        ["mouseup", () => new MouseEvent("mouseup", { ...mouseOptions, buttons: 0 })],
        ["click", () => new MouseEvent("click", { ...mouseOptions, buttons: 0 })],
        ["keydown", () => new KeyboardEvent("keydown", { bubbles: true, cancelable: true, key: "Enter", code: "Enter" })],
        ["keyup", () => new KeyboardEvent("keyup", { bubbles: true, cancelable: true, key: "Enter", code: "Enter" })],
      ]) {
        try {
          currentTarget.dispatchEvent(eventFactory());
        } catch (error) {
          log(`dispatchEvent fallito per ${eventName}`, error);
        }
      }

      try {
        currentTarget.click?.();
      } catch (error) {
        log("click() fallito sul target", error);
      }
    }
    return true;
  }

  function uniqueElements(elements) {
    const seen = new Set();
    return elements.filter((element) => {
      if (!element || seen.has(element)) return false;
      seen.add(element);
      return true;
    });
  }

  function candidateAddIngredientButtons() {
    const candidates = [];

    for (const image of Array.from(document.querySelectorAll("img[src*='glyphs-btn-ingredient']"))) {
      const target = image.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || image;
      candidates.push(target);
    }

    for (const icon of Array.from(document.querySelectorAll("i.mdi.mdi-plus, .mdi-plus"))) {
      const target = icon.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || icon;
      candidates.push(target);
    }

    const ingredientGroups = Array.from(document.querySelectorAll(".create-recipe-ingredient-group-content, .ingredient-group, .create-recipe-ingredient-list"))
      .filter((element) => /ingredienti generali|general ingredients/i.test(normalizeText(element.textContent)));

    for (const group of ingredientGroups) {
      const toolbarButtons = Array.from(group.querySelectorAll(".ingredient-group-app-bar button, .v-toolbar__content button, button, [role='button']"));
      candidates.push(...toolbarButtons.filter((button) => {
        if (!(button instanceof HTMLElement)) return false;
        const text = normalizeText([
          button.textContent,
          button.getAttribute("aria-label"),
          button.getAttribute("title"),
          button.className,
          button.querySelector("img")?.getAttribute("src"),
          button.querySelector("i")?.className,
        ].filter(Boolean).join(" ")).toLowerCase();
        return text.includes("glyphs-btn-ingredient")
          || text.includes("inserire ingredienti")
          || text.includes("aggiungi ingrediente")
          || text.includes("add ingredient")
          || text.includes("mdi-plus")
          || text.includes("btn-ingredient");
      }));

      const toolbar = group.querySelector(".v-toolbar__content");
      if (toolbar) {
        const rightButtons = Array.from(toolbar.querySelectorAll("button, [role='button']"));
        candidates.push(...rightButtons);
      }
    }

    const selectors = [
      "button[aria-label*='ingred' i]",
      "button[title*='ingred' i]",
      "button[data-testid*='ingred' i]",
      "button[class*='ingred' i]",
      "button:has(img[src*='glyphs-btn-ingredient'])",
      "button:has(img[src*='btn-ingredient'])",
      "button:has(img[src*='btn-add'])",
      "[role='button'][aria-label*='ingred' i]",
      "[role='button'][title*='ingred' i]",
      "[role='button'][data-testid*='ingred' i]",
      "div[role='button'][aria-label*='ingred' i]",
      "div[title*='ingred' i]",
      "span[title*='ingred' i]",
      ".v-btn[aria-label*='ingred' i]",
      ".v-btn[title*='ingred' i]",
    ];

    for (const selector of selectors) {
      candidates.push(...Array.from(document.querySelectorAll(selector)));
    }

    const textButtons = Array.from(document.querySelectorAll("button, a, [role='button']"))
      .filter((element) => /inserire ingredienti|aggiungi ingrediente|add ingredient|nuovo ingrediente/i.test(normalizeText(element.textContent)));
    candidates.push(...textButtons);

    return uniqueElements(candidates)
      .filter((element) => element instanceof HTMLElement)
      .filter((element) => !isElementDisabled(element));
  }

  async function tryAddIngredientButton(fieldConfig, previousCount) {
    const candidates = candidateAddIngredientButtons();

    for (const candidate of candidates) {
      clickElementRobust(candidate);
      await sleep(400);

      const fields = findAllFields(fieldConfig);
      if (fields.length > previousCount) {
        return true;
      }
    }

    return false;
  }

  function clickIngredientRowAction() {
    const groups = visibleElements(".ingredient-group, .create-recipe-ingredient-group-content, .create-recipe-ingredient-list")
      .filter((element) => /ingredienti generali|general ingredients/i.test(normalizeText(element.textContent)));

    for (const group of groups) {
      const explicitPlusButton = Array.from(group.querySelectorAll(".insert-row button, .insert-row [role='button'], button, [role='button']"))
        .find((element) => {
          if (!(element instanceof HTMLElement)) return false;
          if (isElementDisabled(element)) return false;
          const rect = element.getBoundingClientRect();
          if (rect.width <= 0 || rect.height <= 0) return false;
          const text = normalizeText([
            element.textContent,
            element.getAttribute("aria-label"),
            element.getAttribute("title"),
            element.className,
          ].filter(Boolean).join(" ")).toLowerCase();
          return text.includes("mdi-plus") || text === "+" || text.includes("insert-row") || text.includes("primary--text");
        });

      if (explicitPlusButton) {
        clickElementRobust(explicitPlusButton);
        return true;
      }
    }

    const anchors = visibleElements("div, span, p, h1, h2, h3, h4, h5, h6")
      .filter((element) => /ingredienti generali|general ingredients/i.test(normalizeText(element.textContent)));

    for (const anchor of anchors) {
      const anchorRect = anchor.getBoundingClientRect();
      const candidates = visibleElements("button, a, [role='button'], [tabindex], .v-btn, .btn, svg, i, span, div")
        .filter((element) => {
          if (element === anchor || anchor.contains(element)) return false;
          if (isElementDisabled(element)) return false;

          const clickable = element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
          if (clickable === anchor || anchor.contains(clickable)) return false;

          const rect = clickable.getBoundingClientRect();
          const verticallyAligned = rect.top <= anchorRect.bottom + 36 && rect.bottom >= anchorRect.top - 36;
          const isToRight = rect.left >= anchorRect.right - 24;
          const isReasonableSize = rect.width > 12 && rect.height > 12;
          return verticallyAligned && isToRight && isReasonableSize;
        })
        .map((element) => {
          const clickable = element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
          const rect = clickable.getBoundingClientRect();
          const text = normalizeText([
            clickable.textContent,
            clickable.getAttribute?.("aria-label"),
            clickable.getAttribute?.("title"),
            clickable.getAttribute?.("data-testid"),
            clickable.className,
            element.tagName,
          ].filter(Boolean).join(" ")).toLowerCase();
          let score = 0;
          if (text.includes("ingred")) score += 4;
          if (text.includes("add")) score += 2;
          if (text.includes("aggiungi")) score += 2;
          if (text.includes("plus") || text.includes("icon")) score += 1;
          score += Math.max(0, Math.round(rect.left / 100));
          return { element: clickable, score };
        })
        .sort((left, right) => right.score - left.score);

      if (candidates[0]?.element) {
        clickElementRobust(candidates[0].element);
        return true;
      }
    }

    return false;
  }

  function clickAddIngredientButton() {
    const glyphImage = visibleElements("img[src*='glyphs-btn-ingredient']")[0];
    if (glyphImage) {
      const target = glyphImage.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || glyphImage;
      clickElementRobust(target);
      return true;
    }

    const selectors = [
      "button[aria-label*='ingred' i]",
      "button[title*='ingred' i]",
      "button[data-testid*='ingred' i]",
      "button[class*='ingred' i]",
      "button:has(img[src*='glyphs-btn-ingredient'])",
      "[role='button'][aria-label*='ingred' i]",
      "[role='button'][title*='ingred' i]",
      "[role='button'][data-testid*='ingred' i]",
      "div[role='button'][aria-label*='ingred' i]",
      "div[title*='ingred' i]",
      "span[title*='ingred' i]",
      ".v-btn[aria-label*='ingred' i]",
      ".v-btn[title*='ingred' i]",
    ];

    for (const selector of selectors) {
      const target = visibleElements(selector).find((element) => !isElementDisabled(element));
      if (target) {
        clickElementRobust(target);
        return true;
      }
    }

    if (clickIngredientRowAction()) {
      return true;
    }

    if (clickByText(/aggiungi ingrediente|add ingredient|nuovo ingrediente|ingrediente/i)) {
      return true;
    }

    const candidates = visibleElements("button, [role='button'], a, div, span, i")
      .filter((element) => !isElementDisabled(element))
      .map((element) => {
        const clickableAncestor = element.closest("button, a, [role='button'], [tabindex], .v-btn, .btn") || element;
        const text = normalizeText([
          element.textContent,
          element.getAttribute("aria-label"),
          element.getAttribute("title"),
          element.getAttribute("data-testid"),
          element.className,
          element.parentElement?.textContent,
          clickableAncestor?.textContent,
          clickableAncestor?.getAttribute?.("aria-label"),
          clickableAncestor?.getAttribute?.("title"),
          clickableAncestor?.getAttribute?.("data-testid"),
          clickableAncestor?.className,
        ].filter(Boolean).join(" ")).toLowerCase();
        const iconText = normalizeText(element.textContent);
        let score = 0;
        if (text.includes("ingred")) score += 4;
        if (text.includes("add")) score += 2;
        if (text.includes("aggiungi")) score += 2;
        if (iconText === "+") score += 1;
        if (String(clickableAncestor?.className || "").toLowerCase().includes("btn")) score += 1;
        return { element: clickableAncestor, score };
      })
      .filter((candidate) => candidate.score > 0)
      .sort((left, right) => right.score - left.score);

    if (candidates[0]?.element) {
      clickElementRobust(candidates[0].element);
      return true;
    }

    return false;
  }

  async function clickWizardNextAndWait(expectedFieldConfig, timeoutMs = 20000) {
    if (!clickByText(/avanti|next|continua|continue|prosegui/i)) {
      throw new Error(`Bottone avanti non trovato. Campi visibili:\n${debugVisibleFields()}`);
    }

    await sleep(1200);
    const nextField = await waitForField(expectedFieldConfig, timeoutMs);
    if (!nextField) {
      throw new Error(`La schermata successiva non contiene i campi attesi. Campi visibili:\n${debugVisibleFields()}`);
    }
    return nextField;
  }

  async function navigateToCreateRecipeIfNeeded() {
    if (window.location.href.startsWith(TARGET_URL)) return;

    if (clickByText(/crea ricetta|create recipe/i)) {
      await sleep(1500);
      return;
    }

    window.location.href = TARGET_URL;
  }

  async function applyRecipeToMonsieurCuisine(recipe) {
    await navigateToCreateRecipeIfNeeded();
    await sleep(1500);

    const timePageFieldConfig = {
      selectors: [
        "input[placeholder*='h' i]",
        "input[placeholder*='min' i]",
        "input[placeholder*='preparazione' i]",
        "input[placeholder*='tempo' i]",
      ],
      patterns: [
        "h.",
        "min",
        "tempo necessario alla preparazione degli ingredienti",
        "tempo complessivo",
        "preparazione",
        "cottura",
        "tempo",
      ],
    };
    const ingredientPageFieldConfig = {
      selectors: [
        ".create-recipe-ingredient-list",
        ".create-recipe-ingredient-group-content",
        ".ingredient-group-app-bar",
        ".serving-ingredient",
        "img[src*='glyphs-btn-ingredient']",
        "textarea[name*='ingredient']",
        "input[name*='ingredient']",
        "textarea[placeholder*='ingrediente' i]",
        "input[placeholder*='ingrediente' i]",
      ],
      patterns: ["ingrediente", "ingredient"],
    };
    const stepPageFieldConfig = stepListPageMarkerConfig();

    const titleField = await waitForField({
      selectors: [
        "input[name='title']",
        "input[placeholder*='titolo' i]",
        "input[placeholder*='title' i]",
        "textarea[placeholder*='titolo' i]",
        "textarea[placeholder*='title' i]",
        "[contenteditable='true']",
        "[role='textbox']",
      ],
      patterns: [
        "titolo",
        "title",
        "nome ricetta",
        "nome della ricetta",
        "nome alla ricetta",
        "dai un nome alla ricetta",
        "recipe name",
        "recipe title",
      ],
    }, 20000);
    if (!titleField) {
      throw new Error(`Campo titolo non trovato nella pagina Monsieur Cuisine. Campi visibili:\n${debugVisibleFields()}`);
    }

    setNativeValue(titleField, recipe.title);
    const briefDescriptionField = findField({
      selectors: [
        "textarea[name*='description']",
        "textarea[placeholder*='breve descrizione' i]",
        "textarea[placeholder*='descrizione' i]",
        "textarea",
        "[contenteditable='true']",
        "[role='textbox']",
      ],
      patterns: ["breve descrizione", "inserisci qui una breve descrizione", "short description", "descrizione"],
    });
    if (briefDescriptionField) {
      setNativeValue(briefDescriptionField, buildShortDescription(recipe));
    }

    const servingsField = findField({
      selectors: [
        "input[name*='serving']",
        "input[placeholder*='porzioni' i]",
        "input[placeholder*='servings' i]",
      ],
      patterns: ["porzioni", "servings", "persone", "dosi"],
    });
    if (servingsField && recipe.yieldText) {
      setNativeValue(servingsField, recipe.yieldText);
    }

    if (recipe.totalTimeMinutes) {
      const totalTimeField = findField({
        selectors: [
          "input[name*='totalTime']",
          "input[name*='duration']",
          "input[placeholder*='tempo totale' i]",
        ],
        patterns: ["tempo totale", "durata", "duration", "total time", "tempo"],
      });
      if (totalTimeField) {
        setNativeValue(totalTimeField, String(recipe.totalTimeMinutes));
      }
    }

    await clickWizardNextAndWait(timePageFieldConfig, 20000);

    fillTimeWizardPage(recipe);

    await clickWizardNextAndWait(ingredientPageFieldConfig, 20000);

    const ingredientEntries = recipe.structuredIngredients.length ? recipe.structuredIngredients : recipe.ingredients;

    for (let index = 0; index < ingredientEntries.length; index += 1) {
      await fillIngredient(ingredientEntries[index], index);
      await sleep(200);
    }

    await settleIngredientPage();

    await clickWizardNextAndWait(stepPageFieldConfig, 20000);

    for (let index = 0; index < recipe.steps.length; index += 1) {
      await fillStep(recipe.steps[index], index);
      await sleep(250);
    }

    return true;
  }

  async function initMonsieurCuisineBridge() {
    let panel = null;

    const renderPanel = async () => {
      panel?.remove();

      let resolved;
      let recipe;
      try {
        resolved = await resolveConfirmedJob();
        recipe = await fetchRecipeFromApp(resolved.jobId);
      } catch (error) {
        log("Nessuna ricetta confermata pronta", error);
        panel = createUnavailablePanel(
          error.message,
          () => {
            renderPanel().catch((retryError) => log("Retry panel failed", retryError));
          },
          () => panel?.remove()
        );
        return;
      }

      panel = createRecipePanel(
        recipe,
        async () => {
          try {
            await applyRecipeToMonsieurCuisine(recipe);
            panel.querySelector("p").innerHTML = `<strong>Form compilato.</strong><br>Ricetta ${escapeHtml(resolved.jobId.slice(0, 8))} letta da MonsieurAPP (${escapeHtml(recipe.selectedReviewMode)}). Controlla i campi e premi Salva manualmente.`;
            if (resolved.source === "hash" && window.location.hash.includes("mc-import=")) {
              history.replaceState(null, document.title, window.location.pathname + window.location.search);
            }
          } catch (error) {
            window.alert(`Compilazione non riuscita: ${error.message}`);
          }
        },
        () => panel?.remove()
      );
    };

    createFab("MonsieurAPP", () => {
      renderPanel().catch((error) => log("Render panel failed", error));
    });

    await renderPanel();
  }

  if (/(^|\.)monsieur-cuisine\.com$/i.test(window.location.hostname)) {
    initMonsieurCuisineBridge();
  }
})();
