// ==UserScript==
// @name         Monsieur Cuisine Bridge
// @namespace    https://monsieurapp.local
// @version      0.2.12.1
// @description  Legge la ricetta confermata da MonsieurAPP e compila il form Monsieur Cuisine nel browser gia' autenticato.
// @match        https://www.monsieur-cuisine.com/*
// @match        https://monsieur-cuisine.com/*
// @match        https://*.monsieur-cuisine.com/*
// @grant        GM_addStyle
// @grant        GM_xmlhttpRequest
// @connect      *
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

(function () {
  "use strict";

  const CONFIGURED_APP_BASE_URL = "__APP_BASE_URL__";
  const APP_BASE_URL = CONFIGURED_APP_BASE_URL === "__APP_BASE_URL__"
    ? "http://127.0.0.1:8000"
    : CONFIGURED_APP_BASE_URL;
  const TARGET_URL = "https://www.monsieur-cuisine.com/it/create-recipe?devices=mc-smart";
  const SCALE_PROGRAM_LABEL = "Bilancia";
  const CUSTOM_COOKING_PROGRAM_LABEL = "Cottura personalizzata";
  const STEP_PROGRAM_SWITCH_SELECTOR = "input[role='switch'][type='checkbox'], input[type='checkbox'][role='switch'], input[aria-checked][type='checkbox']";
  const CUSTOM_COOKING_TEMPERATURE_STEPS = [0, 37, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120, 125, 130];

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
        onerror: () => reject(new Error(`Impossibile raggiungere MonsieurAPP su ${APP_BASE_URL}.`)),
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

  function normalizeStepWeightUnit(unit) {
    const normalized = normalizeText(unit).toLowerCase();
    if (!normalized) {
      return null;
    }

    if (["g", "gr", "grammo", "grammi"].includes(normalized)) {
      return "g";
    }
    if (normalized === "kg") {
      return "kg";
    }
    return null;
  }

  function normalizeStepTargetedWeight(value, unit) {
    if (value == null || value === "") {
      return null;
    }

    let parsedValue = null;
    let parsedUnit = normalizeStepWeightUnit(unit);

    if (typeof value === "number" && Number.isFinite(value)) {
      parsedValue = value;
    } else {
      const normalized = normalizeText(value).toLowerCase().replace(/,/g, ".");
      const match = normalized.match(/(-?\d+(?:\.\d+)?)(?:\s*(kg|g|gr|grammi?|grammo))?/);
      if (match) {
        parsedValue = Number(match[1]);
        if (!parsedUnit && match[2]) {
          parsedUnit = normalizeStepWeightUnit(match[2]);
        }
      }
    }

    if (!Number.isFinite(parsedValue) || parsedValue <= 0) {
      return null;
    }

    if (parsedUnit === "kg") {
      parsedValue *= 1000;
    }

    return Math.max(1, Math.round(parsedValue));
  }

  function formatStepTargetedWeight(step) {
    if (step?.targetedWeight == null) {
      return "";
    }
    return `${step.targetedWeight} ${step.targetedWeightUnit || "g"}`;
  }

  function stepContainsWeighingCue(step, title = "", description = "", originalProgram = normalizeStepProgram(step)) {
    const weighingContext = normalizeText([
      originalProgram,
      formatStepTargetedWeight(step),
      step?.parametersSummary,
      title,
      description,
    ].filter(Boolean).join(" ")).toLowerCase();

    return originalProgram.toLowerCase() === SCALE_PROGRAM_LABEL.toLowerCase()
      || /\bbilancia\b|\bpesa(?:re|ta|te|to|ti)?\b|\bpesare\b|\bgramm(?:o|i)?\b|\b\d+(?:[.,]\d+)?\s*(?:g|gr|grammi?)\b/.test(weighingContext);
  }

  function resolveInternalStepProgram(step, title = "", description = "", originalProgram = normalizeStepProgram(step)) {
    return stepContainsWeighingCue(step, title, description, originalProgram)
      ? SCALE_PROGRAM_LABEL
      : CUSTOM_COOKING_PROGRAM_LABEL;
  }

  function isCustomCookingProgram(step) {
    return normalizeText(step?.selectedProgram || step?.originalProgram || "").toLowerCase() === CUSTOM_COOKING_PROGRAM_LABEL.toLowerCase();
  }

  function isScaleProgram(step) {
    return normalizeText(step?.selectedProgram || step?.originalProgram || "").toLowerCase() === SCALE_PROGRAM_LABEL.toLowerCase();
  }

  function normalizeExportedStep(step, index) {
    const environment = normalizeStepEnvironment(step);
    const title = normalizeText(step?.description || step?.detailedInstructions || `Passaggio ${index + 1}`);
    const description = normalizeText(step?.detailedInstructions || step?.description || title);
    const isDescriptive = environment !== "mc";
    const originalProgram = normalizeStepProgram(step);
    const selectedProgram = isDescriptive ? null : resolveInternalStepProgram(step, title, description, originalProgram);
    const targetedWeight = isDescriptive
      ? null
      : (
        normalizeStepTargetedWeight(step?.targetedWeight, step?.targetedWeightUnit)
        ?? (stepContainsWeighingCue(step, title, description, originalProgram)
          ? normalizeStepTargetedWeight(step?.parametersSummary || description)
          : null)
      );

    return {
      title: title || `Passaggio ${index + 1}`,
      description: description || title || `Passaggio ${index + 1}`,
      durationSeconds: isDescriptive ? null : step?.durationSeconds,
      temperatureC: isDescriptive ? null : step?.temperatureC,
      speed: isDescriptive ? null : step?.speed,
      targetedWeight,
      targetedWeightUnit: targetedWeight != null ? "g" : null,
      reverse: isDescriptive ? false : Boolean(step?.reverse),
      environment,
      isDescriptive,
      isTechnicalOnly: false,
      originalProgram: originalProgram || null,
      selectedProgram,
    };
  }

  function stepHasTechnicalPayload(step) {
    if (!step || step.isDescriptive || step.environment === "external") {
      return false;
    }

    return Boolean(
      normalizeText(step.selectedProgram || step.originalProgram || "")
      || step.durationSeconds != null
      || step.temperatureC != null
      || normalizeText(step.speed || "")
      || step.targetedWeight != null
      || step.reverse
    );
  }

  function buildDescriptiveCompanionStep(step) {
    return {
      ...step,
      durationSeconds: null,
      temperatureC: null,
      speed: null,
      targetedWeight: null,
      targetedWeightUnit: null,
      reverse: false,
      environment: "external",
      isDescriptive: true,
      isTechnicalOnly: false,
      originalProgram: null,
      selectedProgram: null,
    };
  }

  function buildTechnicalCompanionStep(step) {
    return {
      ...step,
      isDescriptive: false,
      isTechnicalOnly: true,
    };
  }

  function shouldSplitTechnicalStep(step) {
    return stepHasTechnicalPayload(step) && isCustomCookingProgram(step);
  }

  function expandNormalizedStep(step) {
    if (!shouldSplitTechnicalStep(step)) {
      return [step];
    }

    return [
      buildDescriptiveCompanionStep(step),
      buildTechnicalCompanionStep(step),
    ];
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
    const normalizedSteps = rawSteps
      .filter((step) => step && typeof step === "object")
      .map((step, index) => normalizeExportedStep(step, index))
      .flatMap((step) => expandNormalizedStep(step));
    const descriptiveStepCount = normalizedSteps.filter((step) => step.isDescriptive).length;

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
      steps: normalizedSteps,
      descriptiveStepCount,
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
    if (recipe.descriptiveStepCount) {
      log(`Esportati ${recipe.descriptiveStepCount} passaggi descrittivi oltre agli step strutturati.`);
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

  function readSelectLikeFieldValue(element) {
    if (!element) return "";

    const slot = element.closest(".v-select__slot, .v-input__slot, .v-input, .v-select, [role='combobox']");
    const hiddenInput = slot?.querySelector("input[type='hidden']");
    return normalizeText(hiddenInput?.value || element.value || slot?.textContent || element.textContent || "");
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
    const descriptiveStepsNote = recipe.descriptiveStepCount
      ? `<br>${recipe.descriptiveStepCount} passaggi esportati come descrittivi`
      : "";

    return createPanel({
      title: "Ricetta confermata pronta",
      bodyHtml: `<strong>${escapeHtml(recipe.title)}</strong><br>${recipe.ingredients.length} ingredienti, ${recipe.steps.length} passaggi esportati verso Monsieur Cuisine${descriptiveStepsNote}`,
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
      note: `Istanza configurata nello script: ${escapeHtml(APP_BASE_URL)}. Se hai cambiato deploy o dominio, riscarica lo script da quella pagina e reinstallalo sopra la versione precedente.`,
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

  function escapeRegExp(value) {
    return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
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

  function findFieldWithin(root, { selectors = [], patterns = [] } = {}) {
    for (const selector of selectors) {
      const target = visibleElementsWithin(root, selector)[0];
      if (target) return target;
    }

    const normalizedPatterns = patterns.map((pattern) => normalizeText(pattern).toLowerCase()).filter(Boolean);
    if (!normalizedPatterns.length) return null;

    const candidates = visibleElementsWithin(root, EDITABLE_FIELD_SELECTOR)
      .map((element) => {
        const haystack = collectFieldText(element);
        const score = normalizedPatterns.reduce((total, pattern) => total + Number(haystack.includes(pattern)), 0);
        return { element, score };
      })
      .filter((candidate) => candidate.score > 0)
      .sort((left, right) => right.score - left.score);

    return candidates[0]?.element || null;
  }

  function countPatternMatches(haystack, patterns = []) {
    return patterns.reduce((total, pattern) => total + Number(haystack.includes(normalizeText(pattern).toLowerCase())), 0);
  }

  function collectScopedFieldText(element) {
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

    const fieldContainer = element.closest(
      ".v-input, .v-input__control, .v-input__slot, .v-select, .v-autocomplete, .v-text-field, .v-slider, .v-selection-control, [role='group']"
    );
    if (fieldContainer && fieldContainer !== element) {
      chunks.push(fieldContainer.getAttribute?.("data-testid"));
      chunks.push(fieldContainer.getAttribute?.("aria-label"));
      chunks.push(fieldContainer.textContent || "");
    }

    return normalizeText(chunks.filter(Boolean).join(" ")).toLowerCase();
  }

  function getElementPatternScore(element, includePatterns = [], excludePatterns = [], textExtractor = collectFieldText) {
    const haystack = textExtractor(element);
    const includeScore = countPatternMatches(haystack, includePatterns);
    const excludeScore = countPatternMatches(haystack, excludePatterns);
    return includeScore - (excludeScore * 3);
  }

  function selectBestElement(elements, includePatterns = [], excludePatterns = [], options = {}) {
    const textExtractor = options.textExtractor || collectFieldText;
    const requireIncludeMatch = options.requireIncludeMatch === true;
    const uniqueCandidates = uniqueElements(elements).filter((element) => isVisibleElement(element));
    if (!uniqueCandidates.length) {
      return null;
    }

    const ranked = uniqueCandidates
      .map((element) => ({
        element,
        includeScore: countPatternMatches(textExtractor(element), includePatterns),
        excludeScore: countPatternMatches(textExtractor(element), excludePatterns),
        score: getElementPatternScore(element, includePatterns, excludePatterns, textExtractor),
      }))
      .sort((left, right) => right.includeScore - left.includeScore || left.excludeScore - right.excludeScore || right.score - left.score);

    if (includePatterns.length) {
      const positiveMatch = ranked.find((candidate) => candidate.includeScore > 0);
      if (positiveMatch) {
        return positiveMatch.element;
      }
      if (requireIncludeMatch) {
        return null;
      }
    }

    return ranked.find((candidate) => candidate.score >= 0)?.element || ranked[0]?.element || null;
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

  function summarizeDebugElement(element) {
    if (!(element instanceof Element)) {
      return "nessuno";
    }

    const tagName = element.tagName.toLowerCase();
    const descriptor = [
      element.getAttribute("type") ? `type=${element.getAttribute("type")}` : "",
      element.getAttribute("role") ? `role=${element.getAttribute("role")}` : "",
      element.getAttribute("name") ? `name=${element.getAttribute("name")}` : "",
      element.id ? `id=${element.id}` : "",
      element.getAttribute("placeholder") ? `placeholder=${element.getAttribute("placeholder")}` : "",
      element.getAttribute("aria-label") ? `aria=${element.getAttribute("aria-label")}` : "",
      element.getAttribute("aria-valuenow") ? `aria-valuenow=${element.getAttribute("aria-valuenow")}` : "",
      readFieldValue(element) ? `value=${readFieldValue(element)}` : "",
    ].filter(Boolean).join(" ");
    const scopedText = truncateText(
      element.getAttribute("role") === "slider"
        ? collectStepSliderText(element)
        : collectScopedFieldText(element),
      180,
    ) || "senza testo";

    return `${tagName}${descriptor ? ` ${descriptor}` : ""} -> ${scopedText}`;
  }

  function formatDebugElementGroup(title, elements, limit = 6) {
    const visibleCandidates = uniqueElements(elements).filter((element) => isVisibleElement(element) || isSliderLikeVisible(element)).slice(0, limit);
    if (!visibleCandidates.length) {
      return `${title}: nessuno`;
    }

    return `${title}:\n${visibleCandidates.map((element, index) => `${index + 1}. ${summarizeDebugElement(element)}`).join("\n")}`;
  }

  function debugCustomCookingControls(root = findStepEditorRoot()) {
    const rotationCandidates = uniqueElements([
      ...visibleElementsWithin(root, "input[type='radio'], input[type='checkbox']"),
      ...visibleElementsWithin(root, "label, button, [role='button'], [role='radio']"),
    ]).filter((element) => /rot|senso|orario|antiorario|reverse|clock|direction|mdi-rotate|mdi-undo|mdi-redo/.test(
      normalizeText([
        collectFieldText(element),
        collectScopedFieldText(element),
        element.textContent,
        element.className,
      ].filter(Boolean).join(" ")).toLowerCase()
    ));

    return [
      `Temperatura selezionata: ${summarizeDebugElement(findStepTemperatureInput(root) || findStepTemperatureSlider(root))}`,
      `Velocita selezionata: ${summarizeDebugElement(findStepSpeedField(root) || findStepSpeedSlider(root))}`,
      `Rotazione selezionata: ${summarizeDebugElement(findStepRotationInput(root, true) || findStepRotationButton(root, true) || findStepReverseToggle(root))}`,
      formatDebugElementGroup("Candidati temperatura", [
        ...visibleElementsWithin(root, "input[name*='temp' i]"),
        ...visibleElementsWithin(root, "input[placeholder*='°' i]"),
        ...visibleElementsWithin(root, "input[placeholder*='temperatura' i]"),
        ...visibleElementsWithin(root, "input[aria-label*='temperatura' i]"),
        ...visibleElementsWithin(root, "input[aria-label*='temp' i]"),
        ...visibleElementsWithin(root, "input[type='number']"),
        ...visibleElementsWithin(root, "input[inputmode='numeric']"),
        ...visibleElementsWithin(root, "input[inputmode='decimal']"),
      ]),
      formatDebugElementGroup("Candidati slider temperatura", [
        ...getStepParameterSliderCandidates(root, "temperature"),
      ]),
      formatDebugElementGroup("Candidati velocita", [
        ...visibleElementsWithin(root, "select[name*='speed' i]"),
        ...visibleElementsWithin(root, "input[name*='speed' i]"),
        ...visibleElementsWithin(root, "input[placeholder*='veloc' i]"),
        ...visibleElementsWithin(root, "input[aria-label*='veloc' i]"),
        ...visibleElementsWithin(root, "input[readonly]"),
        ...visibleElementsWithin(root, "[role='combobox'] input"),
      ]),
      formatDebugElementGroup("Candidati slider velocita", [
        ...getStepParameterSliderCandidates(root, "speed"),
      ]),
      formatDebugElementGroup("Candidati rotazione", rotationCandidates),
    ].join("\n\n");
  }

  function debugScaleControls(root = findStepEditorRoot()) {
    return [
      `Peso selezionato: ${summarizeDebugElement(findStepTargetedWeightField(root))}`,
      formatDebugElementGroup("Candidati peso", getStepTargetedWeightCandidates(root)),
    ].join("\n\n");
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

  function isSliderLikeVisible(element) {
    if (!(element instanceof Element)) {
      return false;
    }

    const style = window.getComputedStyle(element);
    if (style.visibility === "hidden" || style.display === "none") {
      return false;
    }

    const rect = element.getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0) {
      return true;
    }

    const container = element.closest(".parameter-temperature-step-field, .parameter-speed-field, .v-input__slider, .v-input, .v-slider");
    if (!container || container === element) {
      return false;
    }

    const containerStyle = window.getComputedStyle(container);
    const containerRect = container.getBoundingClientRect();
    return containerStyle.visibility !== "hidden"
      && containerStyle.display !== "none"
      && containerRect.width > 0
      && containerRect.height > 0;
  }

  function getStepSliderCandidates(root = findStepEditorRoot()) {
    const scope = root instanceof Element ? root : document;
    return uniqueElements([
      ...Array.from(scope.querySelectorAll("[role='slider']")),
      ...Array.from(scope.querySelectorAll(".v-slider__thumb-container[role='slider']")),
    ]).filter((element) => isSliderLikeVisible(element));
  }

  function collectStepSliderText(element) {
    if (!element) {
      return "";
    }

    const container = element.closest(".parameter-temperature-step-field, .parameter-speed-field")
      || element.closest(".v-input__slider")
      || element.closest(".v-input")
      || element.closest(".v-slider")
      || element;
    const relatedNodes = uniqueElements([
      element,
      container,
      container.querySelector?.("label"),
      container.querySelector?.(".v-label"),
    ]);
    const imageSources = container
      ? Array.from(container.querySelectorAll("img")).map((image) => image.getAttribute("src")).filter(Boolean)
      : [];

    return normalizeText([
      collectScopedFieldText(element),
      ...relatedNodes.flatMap((node) => [
        node?.className,
        node?.getAttribute?.("aria-label"),
        node?.getAttribute?.("data-testid"),
        node?.textContent,
      ]),
      ...imageSources,
    ].filter(Boolean).join(" ")).toLowerCase();
  }

  function getStepParameterSliderCandidates(root = findStepEditorRoot(), parameter) {
    const config = parameter === "speed"
      ? {
          includePatterns: ["parameter-speed", "control-speed", "veloc", "speed", "rpm"],
          excludePatterns: ["parameter-temperature", "control-temperature", "temperatura", "temperature", "temp", "timer", "tempo", "durata", "duration"],
          containerSelector: ".parameter-speed-field",
        }
      : {
          includePatterns: ["parameter-temperature", "control-temperature", "temperatura", "temperature", "temp"],
          excludePatterns: ["parameter-speed", "control-speed", "veloc", "speed", "timer", "tempo", "durata", "duration"],
          containerSelector: ".parameter-temperature-step-field",
        };
    const ranked = getStepSliderCandidates(root)
      .map((element) => {
        const signature = collectStepSliderText(element);
        const includeScore = countPatternMatches(signature, config.includePatterns);
        const excludeScore = countPatternMatches(signature, config.excludePatterns);
        const containerScore = element.closest(config.containerSelector) ? 5 : 0;

        return {
          element,
          includeScore,
          excludeScore,
          containerScore,
          score: includeScore + containerScore - (excludeScore * 3),
        };
      })
      .sort((left, right) => right.containerScore - left.containerScore || right.includeScore - left.includeScore || left.excludeScore - right.excludeScore || right.score - left.score);
    const positiveMatches = ranked.filter((candidate) => candidate.includeScore > 0 || candidate.containerScore > 0);
    if (positiveMatches.length) {
      return positiveMatches.map((candidate) => candidate.element);
    }

    const neutralMatches = ranked.filter((candidate) => candidate.excludeScore === 0);
    if (neutralMatches.length) {
      return neutralMatches.map((candidate) => candidate.element);
    }

    return ranked.length === 1 ? [ranked[0].element] : [];
  }

  function findStepParameterSlider(root = findStepEditorRoot(), parameter) {
    return getStepParameterSliderCandidates(root, parameter)[0] || null;
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

  function textMatchesOptionLabel(text, label) {
    const normalizedText = normalizeText(text).toLowerCase();
    const normalizedLabel = normalizeText(label).toLowerCase();
    if (!normalizedText || !normalizedLabel) {
      return false;
    }

    if (normalizedText === normalizedLabel) {
      return true;
    }

    return new RegExp(`(^|\\s|\\()${escapeRegExp(normalizedLabel)}($|\\s|\\)|,|\\.|:)`).test(normalizedText);
  }

  function findVisibleOptionByLabels(labels) {
    const normalizedLabels = labels.map((label) => normalizeText(label).toLowerCase()).filter(Boolean);
    if (!normalizedLabels.length) {
      return null;
    }

    const options = uniqueElements(visibleElements("[role='option'], .v-list-item, .v-list-item__title"))
      .map((element) => element.closest("[role='option'], .v-list-item") || element)
      .filter((element) => isVisibleElement(element));

    return options.find((element) => {
      const optionText = normalizeText(element.textContent).toLowerCase();
      return normalizedLabels.some((label) => textMatchesOptionLabel(optionText, label));
    }) || options.find((element) => {
      const optionText = normalizeText(element.textContent).toLowerCase();
      return normalizedLabels.some((label) => optionText.includes(label));
    }) || null;
  }

  function selectNativeOptionByLabels(selectElement, labels) {
    if (!(selectElement instanceof HTMLSelectElement)) {
      return false;
    }

    const option = Array.from(selectElement.options).find((candidate) => {
      const optionText = normalizeText(candidate.label || candidate.text || candidate.value).toLowerCase();
      return labels.some((label) => textMatchesOptionLabel(optionText, label) || optionText.includes(label));
    });
    if (!option) {
      return false;
    }

    selectElement.value = option.value;
    selectElement.dispatchEvent(new Event("input", { bubbles: true }));
    selectElement.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function getSelectLikeFieldTexts(field) {
    if (!field) {
      return [];
    }

    const slot = field.closest(".v-select__slot, .v-input__slot, .v-input, .v-select, .v-autocomplete, [role='combobox']");
    return Array.from(new Set([
      readSelectLikeFieldValue(field),
      field.getAttribute?.("value"),
      slot?.textContent,
      slot?.querySelector?.("input[type='hidden']")?.value,
      slot?.querySelector?.("input:not([type='hidden'])")?.value,
    ].map((value) => normalizeText(value).toLowerCase()).filter(Boolean)));
  }

  function selectLikeFieldMatchesLabels(field, labels) {
    if (!field) {
      return false;
    }

    const normalizedLabels = labels.map((label) => normalizeText(label).toLowerCase()).filter(Boolean);
    if (!normalizedLabels.length) {
      return false;
    }

    const fieldTexts = getSelectLikeFieldTexts(field);
    return normalizedLabels.some((label) => fieldTexts.some((text) => text.includes(label) || textMatchesOptionLabel(text, label)));
  }

  async function settleSelectLikeField(field) {
    if (!field) {
      return false;
    }

    const slot = field.closest(".v-select__slot, .v-input__slot, .v-input, .v-select, .v-autocomplete, [role='combobox']");
    const targets = uniqueElements([
      field,
      slot,
      slot?.querySelector?.("input[type='hidden']"),
      slot?.querySelector?.("input:not([type='hidden'])"),
    ]);

    for (const target of targets) {
      if (!(target instanceof Element)) {
        continue;
      }

      target.dispatchEvent(new Event("input", { bubbles: true }));
      target.dispatchEvent(new Event("change", { bubbles: true }));
    }

    field.blur?.();
    field.dispatchEvent(new Event("blur", { bubbles: true }));
    await sleep(180);
    return true;
  }

  async function setSelectLikeFieldOption(field, optionLabels) {
    if (!field) {
      return false;
    }

    const normalizedLabels = optionLabels.map((label) => normalizeText(label).toLowerCase()).filter(Boolean);
    if (!normalizedLabels.length) {
      return false;
    }

    if (selectNativeOptionByLabels(field, normalizedLabels)) {
      return true;
    }

    const trigger = field.closest("[role='combobox'], .v-select, .v-autocomplete, .v-input, .v-input__slot, .v-select__slot") || field;
    clickElementRobust(trigger);

    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      const option = findVisibleOptionByLabels(normalizedLabels);
      if (option) {
        clickElementRobust(option);
        await sleep(300);
        const selectedValue = readSelectLikeFieldValue(field).toLowerCase();
        if (!selectedValue || normalizedLabels.some((label) => selectedValue.includes(label) || textMatchesOptionLabel(selectedValue, label))) {
          return true;
        }
        return true;
      }
      await sleep(150);
    }

    return false;
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
    const source = normalizeText(step?.title || step?.description || "");
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

  function findStepEditorRoot() {
    const editorField = findField(stepEditorFieldConfig());
    const dialogRoot = editorField?.closest?.(".v-dialog, .v-dialog__content, .v-overlay__content, .menuable__content__active, [role='dialog']");
    if (dialogRoot) {
      return dialogRoot;
    }

    return visibleElements(".v-dialog, .v-dialog__content, .v-overlay__content, .menuable__content__active, [role='dialog']")[0] || document.body;
  }

  function visibleElementsWithin(root, selector) {
    if (!(root instanceof Element)) {
      return visibleElements(selector);
    }

    return Array.from(root.querySelectorAll(selector)).filter((element) => isVisibleElement(element));
  }

  function findStepProgramActivator(root = findStepEditorRoot()) {
    const switchInput = findStepProgramSwitchInput(root);
    if (switchInput) {
      return switchInput.closest("label, .v-input--selection-controls, .v-selection-control, .v-input, .v-input__slot") || switchInput;
    }

    const candidates = uniqueElements([
      ...visibleElementsWithin(root, ".v-input--selection-controls__ripple"),
      ...visibleElementsWithin(root, ".v-input--selection-controls"),
      ...visibleElementsWithin(root, ".v-selection-control"),
    ]);

    return candidates[0] || null;
  }

  function findStepProgramSwitchInput(root = findStepEditorRoot()) {
    return visibleElementsWithin(root, STEP_PROGRAM_SWITCH_SELECTOR)[0] || null;
  }

  function findStepProgramField(root = findStepEditorRoot()) {
    const candidates = uniqueElements([
      ...visibleElementsWithin(root, ".v-select__selections input[readonly]"),
      ...visibleElementsWithin(root, ".v-select input[readonly]"),
      ...visibleElementsWithin(root, "[role='combobox'] input[readonly]"),
      ...visibleElementsWithin(root, "input[readonly][autocomplete='off']"),
    ]).map((element) => ({
      element,
      score: ["programma", "program", "cooking program"].reduce(
        (total, pattern) => total + Number(collectFieldText(element).includes(pattern)),
        0,
      ),
    }));

    candidates.sort((left, right) => right.score - left.score);
    return candidates[0]?.element || null;
  }

  function readSwitchCheckedState(input) {
    if (!input) {
      return false;
    }

    return Boolean(input.checked || input.getAttribute("aria-checked") === "true");
  }

  async function ensureSwitchState(input, expectedChecked) {
    if (!input) {
      return false;
    }

    const desiredState = Boolean(expectedChecked);
    if (readSwitchCheckedState(input) === desiredState) {
      return true;
    }

    const clickableTarget = input.closest("label, .v-input--selection-controls, .v-selection-control, .v-input, .v-input__slot") || input;
    clickElementRobust(clickableTarget);
    await sleep(250);
    if (readSwitchCheckedState(input) === desiredState) {
      return true;
    }

    clickElementRobust(input);
    await sleep(250);
    if (readSwitchCheckedState(input) === desiredState) {
      return true;
    }

    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "checked")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked");
    if (descriptor?.set) {
      descriptor.set.call(input, desiredState);
    } else {
      input.checked = desiredState;
    }
    input.setAttribute("aria-checked", desiredState ? "true" : "false");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
    await sleep(250);

    return readSwitchCheckedState(input) === desiredState;
  }

  function findStepProgramOption(programLabel) {
    return findVisibleOptionByLabels([programLabel]);
  }

  function parseStepSpeedValue(speed) {
    const normalized = normalizeText(speed).toLowerCase();
    if (!normalized) {
      return null;
    }

    const aliases = new Map([
      ["soft", "1"],
      ["velocità soft", "1"],
      ["velocita soft", "1"],
      ["cucchiaio", "1"],
      ["velocità cucchiaio", "1"],
      ["velocita cucchiaio", "1"],
    ]);
    if (aliases.has(normalized)) {
      return aliases.get(normalized);
    }

    const numericMatch = normalized.match(/(\d+(?:[.,]\d+)?)/);
    if (!numericMatch) {
      return null;
    }

    const parsed = Number(numericMatch[1].replace(",", "."));
    if (!Number.isFinite(parsed)) {
      return null;
    }

    const bounded = Math.min(10, Math.max(0, parsed));
    return Number.isInteger(bounded) ? String(bounded) : String(Number(bounded.toFixed(1)));
  }

  function buildSpeedOptionLabels(speedValue) {
    const normalized = normalizeText(speedValue);
    if (!normalized) {
      return [];
    }

    const commaVariant = normalized.replace(".", ",");
    return Array.from(new Set([
      normalized,
      commaVariant,
      `velocita ${normalized}`,
      `velocita ${commaVariant}`,
      `velocità ${normalized}`,
      `velocità ${commaVariant}`,
      `speed ${normalized}`,
      `speed ${commaVariant}`,
    ].map((value) => normalizeText(value).toLowerCase()).filter(Boolean)));
  }

  function resolveSpeedSliderTargets(speedValue) {
    const parsed = Number(String(speedValue ?? "").replace(",", "."));
    if (!Number.isFinite(parsed)) {
      return [];
    }

    const bounded = Math.min(10, Math.max(0, parsed));
    return Array.from(new Set([
      bounded,
      Math.round(bounded),
      Math.floor(bounded),
      Math.ceil(bounded),
    ].filter((value) => Number.isFinite(value))));
  }

  function getStepTotalMinutes(durationSeconds) {
    if (durationSeconds == null) {
      return null;
    }

    return Math.max(1, Math.round(Number(durationSeconds) / 60));
  }

  function getStepDurationParts(durationSeconds) {
    const totalMinutes = getStepTotalMinutes(durationSeconds);
    if (totalMinutes == null) {
      return null;
    }

    return {
      hours: String(Math.floor(totalMinutes / 60)),
      minutes: String(totalMinutes % 60),
    };
  }

  function resolveTemperatureSliderIndex(temperatureC) {
    if (temperatureC == null) {
      return null;
    }

    const target = Number(temperatureC);
    if (!Number.isFinite(target)) {
      return null;
    }

    let bestIndex = 0;
    let bestDistance = Number.POSITIVE_INFINITY;
    CUSTOM_COOKING_TEMPERATURE_STEPS.forEach((value, index) => {
      const distance = Math.abs(value - target);
      if (distance < bestDistance) {
        bestDistance = distance;
        bestIndex = index;
      }
    });
    return bestIndex;
  }

  function resolveCustomCookingTemperatureValue(temperatureC) {
    const targetIndex = resolveTemperatureSliderIndex(temperatureC);
    if (targetIndex == null) {
      return null;
    }

    return CUSTOM_COOKING_TEMPERATURE_STEPS[targetIndex] ?? null;
  }

  function readSliderValue(slider) {
    const rawValues = [
      slider?.getAttribute?.("aria-valuenow"),
      slider?.closest?.(".v-input, .v-slider")?.querySelector?.("input[type='hidden']")?.value,
      slider?.closest?.(".v-input, .v-slider")?.querySelector?.("input:not([type='hidden'])")?.value,
    ];
    const parsedValues = rawValues
      .map((raw) => Number(String(raw ?? "").replace(",", ".")))
      .filter((value) => Number.isFinite(value));

    if (!parsedValues.length) {
      return null;
    }

    const firstValue = parsedValues[0];
    const divergentValue = parsedValues.find((value) => value !== firstValue);
    if (divergentValue != null) {
      return parsedValues[parsedValues.length - 1];
    }

    return firstValue;
  }

  function getSliderBounds(slider) {
    const rawMin = Number(slider?.getAttribute?.("aria-valuemin") || 0);
    const rawMax = Number(slider?.getAttribute?.("aria-valuemax") || 20);
    return {
      min: Number.isFinite(rawMin) ? rawMin : 0,
      max: Number.isFinite(rawMax) ? rawMax : 20,
    };
  }

  function getKeyboardEventCode(key) {
    const keyMap = {
      ArrowLeft: 37,
      ArrowUp: 38,
      ArrowRight: 39,
      ArrowDown: 40,
      Home: 36,
      End: 35,
      PageUp: 33,
      PageDown: 34,
      Enter: 13,
      Tab: 9,
    };

    return keyMap[key] || 0;
  }

  function createKeyboardEventWithCode(eventName, key, code) {
    const keyCode = getKeyboardEventCode(key);
    const event = new KeyboardEvent(eventName, {
      bubbles: true,
      cancelable: true,
      composed: true,
      key,
      code,
    });

    for (const propertyName of ["keyCode", "which", "charCode"]) {
      try {
        Object.defineProperty(event, propertyName, {
          get: () => keyCode,
        });
      } catch (error) {
        // Ignore read-only event property failures.
      }
    }

    return event;
  }

  function dispatchSliderKeyboardEvent(target, key, code) {
    for (const eventName of ["keydown", "keyup"]) {
      target.dispatchEvent(createKeyboardEventWithCode(eventName, key, code));
    }
  }

  function findSliderTrack(slider) {
    return slider?.closest?.(".v-slider")?.querySelector?.(".v-slider__track-container, .v-slider__track-background, .v-input__slot")
      || slider?.parentElement
      || slider;
  }

  function createPointerLikeMouseEvent(eventName, clientX, clientY, buttons = 1) {
    return new MouseEvent(eventName, {
      bubbles: true,
      cancelable: true,
      composed: true,
      view: window,
      clientX,
      clientY,
      screenX: window.screenX + clientX,
      screenY: window.screenY + clientY,
      button: 0,
      buttons,
      detail: eventName === "click" ? 1 : 0,
    });
  }

  function dispatchMouseSequence(target, clientX, clientY) {
    for (const currentTarget of uniqueElements([target, target?.closest?.("button, [role='button']") || null])) {
      if (!currentTarget) {
        continue;
      }

      try {
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mousedown", clientX, clientY, 1));
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mouseup", clientX, clientY, 0));
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("click", clientX, clientY, 0));
      } catch (error) {
        log("Interazione slider fallita", error);
      }
    }
  }

  function dragSliderToRatio(slider, ratio) {
    const track = findSliderTrack(slider);
    const sliderRoot = slider?.closest?.(".v-slider__container, .v-slider, .v-input__slider, .v-input") || track;
    if (!track || !sliderRoot) {
      return false;
    }

    const trackRect = track.getBoundingClientRect();
    if (trackRect.width <= 0 || trackRect.height <= 0) {
      return false;
    }

    const currentValue = readSliderValue(slider);
    const { min, max } = getSliderBounds(slider);
    const currentRatio = Number.isFinite(currentValue) && max !== min
      ? (currentValue - min) / (max - min)
      : 0;
    const clampedCurrentRatio = Math.min(1, Math.max(0, currentRatio));
    const clampedTargetRatio = Math.min(1, Math.max(0, ratio));
    const startX = trackRect.left + (trackRect.width * clampedCurrentRatio);
    const targetX = trackRect.left + (trackRect.width * clampedTargetRatio);
    const clientY = trackRect.top + (trackRect.height / 2);
    const interactionTargets = uniqueElements([sliderRoot, track, slider, window, document]);

    sliderRoot.scrollIntoView?.({ block: "center", inline: "center" });

    for (const currentTarget of interactionTargets) {
      if (!currentTarget?.dispatchEvent) {
        continue;
      }

      try {
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mousemove", startX, clientY, 0));
      } catch (error) {
        log("Preparazione drag slider fallita", error);
      }
    }

    for (const currentTarget of interactionTargets) {
      if (!currentTarget?.dispatchEvent) {
        continue;
      }

      try {
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mousedown", startX, clientY, 1));
      } catch (error) {
        log("mousedown slider fallito", error);
      }
    }

    const intermediateX = startX + ((targetX - startX) / 2);
    for (const currentTarget of interactionTargets) {
      if (!currentTarget?.dispatchEvent) {
        continue;
      }

      try {
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mousemove", intermediateX, clientY, 1));
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mousemove", targetX, clientY, 1));
      } catch (error) {
        log("mousemove slider fallito", error);
      }
    }

    for (const currentTarget of interactionTargets) {
      if (!currentTarget?.dispatchEvent) {
        continue;
      }

      try {
        currentTarget.dispatchEvent(createPointerLikeMouseEvent("mouseup", targetX, clientY, 0));
      } catch (error) {
        log("mouseup slider fallito", error);
      }
    }

    dispatchMouseSequence(track, targetX, clientY);
    return true;
  }

  function collectVueInstances(element) {
    const instances = [];
    const seen = new Set();
    let current = element instanceof Element ? element : null;

    while (current) {
      const maybeVue = current.__vue__;
      if (maybeVue && !seen.has(maybeVue)) {
        seen.add(maybeVue);
        instances.push(maybeVue);
      }
      current = current.parentElement;
    }

    return instances;
  }

  async function trySetVueSliderValue(slider, targetValue) {
    const candidateFields = ["lazyValue", "internalValue", "inputValue", "modelValue", "value"];

    for (const vm of collectVueInstances(slider)) {
      let touched = false;

      for (const fieldName of candidateFields) {
        if (!(fieldName in vm) || typeof vm[fieldName] === "function") {
          continue;
        }

        try {
          vm[fieldName] = targetValue;
          touched = true;
        } catch (error) {
          // Ignore read-only Vue props.
        }
      }

      for (const eventName of ["input", "change", "update:modelValue", "update:model-value", "end", "start"]) {
        try {
          vm.$emit?.(eventName, targetValue);
        } catch (error) {
          // Ignore emit failures on non-component objects.
        }
      }

      try {
        vm.$forceUpdate?.();
      } catch (error) {
        // Ignore forceUpdate failures.
      }

      if (!touched) {
        continue;
      }

      await sleep(100);
      if (readSliderValue(slider) === targetValue) {
        return true;
      }
    }

    return false;
  }

  function clickSliderAtRatio(slider, ratio) {
    const track = findSliderTrack(slider);
    if (!track) {
      return false;
    }

    const rect = track.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) {
      return false;
    }

    const clampedRatio = Math.min(1, Math.max(0, ratio));
    const clientX = rect.left + (rect.width * clampedRatio);
    const clientY = rect.top + (rect.height / 2);
    track.scrollIntoView?.({ block: "center", inline: "center" });
    dispatchMouseSequence(track, clientX, clientY);
    if (track !== slider) {
      dispatchMouseSequence(slider, clientX, clientY);
    }
    return true;
  }

  async function setSliderToIndex(slider, targetIndex) {
    if (!slider || targetIndex == null) {
      return false;
    }

    const { min, max } = getSliderBounds(slider);
    const range = Math.max(1, max - min);
    const boundedTarget = Math.min(max, Math.max(min, Number(targetIndex)));
    let currentValue = readSliderValue(slider);
    if (currentValue === boundedTarget) {
      return true;
    }

    slider.scrollIntoView?.({ block: "center", inline: "center" });
    slider.focus?.();

    if (clickSliderAtRatio(slider, (boundedTarget - min) / range)) {
      await sleep(180);
      currentValue = readSliderValue(slider);
      if (currentValue === boundedTarget) {
        return true;
      }
    }

    if (dragSliderToRatio(slider, (boundedTarget - min) / range)) {
      await sleep(220);
      currentValue = readSliderValue(slider);
      if (currentValue === boundedTarget) {
        return true;
      }
    }

    const resetKey = boundedTarget === max ? "End" : "Home";
    dispatchSliderKeyboardEvent(slider, resetKey, resetKey);
    await sleep(120);

    currentValue = readSliderValue(slider);
    const directionKey = boundedTarget >= (currentValue ?? 0) ? "ArrowRight" : "ArrowLeft";
    for (let stepIndex = 0; stepIndex < range + 3; stepIndex += 1) {
      currentValue = readSliderValue(slider);
      if (currentValue === boundedTarget) {
        break;
      }

      dispatchSliderKeyboardEvent(slider, directionKey, directionKey);
      await sleep(60);
    }

    currentValue = readSliderValue(slider);
    if (currentValue !== boundedTarget) {
      const sliderInput = slider.closest(".v-input, .v-slider")?.querySelector("input[type='hidden'], input:not([type='hidden'])") || null;
      if (sliderInput) {
        setNativeValue(sliderInput, String(boundedTarget));
        sliderInput.dispatchEvent(new Event("input", { bubbles: true }));
        sliderInput.dispatchEvent(new Event("change", { bubbles: true }));
        await sleep(80);
        currentValue = readSliderValue(slider);
      }
    }

    if (currentValue !== boundedTarget && await trySetVueSliderValue(slider, boundedTarget)) {
      currentValue = readSliderValue(slider);
    }

    return currentValue === boundedTarget;
  }

  function findStepTemperatureInput(root = findStepEditorRoot()) {
    return selectBestElement([
      ...visibleElementsWithin(root, "input[name*='temp' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='°' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='temperatura' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='temperatura' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='temp' i]"),
      ...visibleElementsWithin(root, "input[type='number']"),
      ...visibleElementsWithin(root, "input[inputmode='numeric']"),
      ...visibleElementsWithin(root, "input[inputmode='decimal']"),
    ], ["temperatura", "temp", "°"], ["tempo", "durata", "veloc", "speed"], {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    });
  }

  function findStepTemperatureSlider(root = findStepEditorRoot()) {
    return findStepParameterSlider(root, "temperature");
  }

  async function setStepTemperatureField(step, root = findStepEditorRoot()) {
    if (!step || step.temperatureC == null) {
      return false;
    }

    const targetTemperature = resolveCustomCookingTemperatureValue(step.temperatureC) ?? Number(step.temperatureC);
    const input = findStepTemperatureInput(root);
    if (input) {
      const normalizedTemperature = String(targetTemperature);
      await commitTextInputLikeUser(input, normalizedTemperature);
      await ensureFieldValue(input, normalizedTemperature, "Temperatura");
      return true;
    }

    const slider = findStepTemperatureSlider(root);
    if (!slider) {
      return false;
    }

    const { max } = getSliderBounds(slider);
    const targetIndex = resolveTemperatureSliderIndex(targetTemperature);
    const candidateTargets = [];
    if (max <= CUSTOM_COOKING_TEMPERATURE_STEPS.length && targetIndex != null) {
      candidateTargets.push(targetIndex);
    } else {
      candidateTargets.push(targetTemperature);
      if (targetIndex != null && !candidateTargets.includes(targetIndex)) {
        candidateTargets.push(targetIndex);
      }
    }

    for (const candidateTarget of candidateTargets) {
      if (candidateTarget == null) {
        continue;
      }
      if (await setSliderToIndex(slider, candidateTarget)) {
        return true;
      }
    }

    return false;
  }

  function findStepSingleDurationField(root = findStepEditorRoot()) {
    return selectBestElement([
      ...visibleElementsWithin(root, "input[name*='time' i]"),
      ...visibleElementsWithin(root, "input[name*='duration' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='min' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='minut' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='duration' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='tempo' i]"),
    ], ["tempo", "durata", "minuti", "minutes", "minute", "min"], ["temperatura", "temp", "veloc", "speed"], {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    });
  }

  function findStepDurationFields(root = findStepEditorRoot()) {
    const numericInputs = uniqueElements([
      ...visibleElementsWithin(root, "input[type='number']"),
      ...visibleElementsWithin(root, "input[inputmode='numeric']"),
      ...visibleElementsWithin(root, "input[inputmode='decimal']"),
    ]);

    const hoursField = selectBestElement(numericInputs, ["ore", "hour", "hours"], ["temperatura", "temp", "veloc", "speed", "minuti", "minutes", "minute", "min"], {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    });
    const minutesField = selectBestElement(numericInputs.filter((element) => element !== hoursField), ["minuti", "minutes", "minute", "min"], ["temperatura", "temp", "veloc", "speed", "ore", "hour", "hours"], {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    });

    return {
      directDurationField: findStepSingleDurationField(root),
      hoursField,
      minutesField,
    };
  }

  async function setStepDurationFields(step, root = findStepEditorRoot()) {
    const durationParts = getStepDurationParts(step?.durationSeconds);
    if (!durationParts) {
      return false;
    }

    const totalMinutes = getStepTotalMinutes(step?.durationSeconds);
    const { directDurationField, hoursField, minutesField } = findStepDurationFields(root);
    const hasSplitFields = hoursField && minutesField && hoursField !== minutesField;

    if (hasSplitFields) {
      await commitTextInputLikeUser(hoursField, durationParts.hours);
      await ensureFieldValue(hoursField, durationParts.hours, "Ore");
      await commitTextInputLikeUser(minutesField, durationParts.minutes);
      await ensureFieldValue(minutesField, durationParts.minutes, "Minuti");
      return true;
    }

    const singleDurationField = directDurationField || minutesField || hoursField;
    if (!singleDurationField || totalMinutes == null) {
      return false;
    }

    const normalizedDuration = String(totalMinutes);
    await commitTextInputLikeUser(singleDurationField, normalizedDuration);
    await ensureFieldValue(singleDurationField, normalizedDuration, "Durata");
    return true;
  }

  function getStepTargetedWeightCandidates(root = findStepEditorRoot()) {
    return uniqueElements([
      ...visibleElementsWithin(root, "input[name*='weight' i]"),
      ...visibleElementsWithin(root, "input[name*='peso' i]"),
      ...visibleElementsWithin(root, "input[name*='target' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='peso' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='weight' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='peso' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='weight' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='target' i]"),
      ...visibleElementsWithin(root, "input[type='number']"),
      ...visibleElementsWithin(root, "input[inputmode='numeric']"),
      ...visibleElementsWithin(root, "input[inputmode='decimal']"),
    ]);
  }

  function findStepTargetedWeightField(root = findStepEditorRoot()) {
    const candidates = getStepTargetedWeightCandidates(root);
    const excludePatterns = [
      "temperatura",
      "temp",
      "veloc",
      "speed",
      "tempo",
      "durata",
      "programma",
      "program",
      "titolo",
      "title",
      "descrizione",
      "description",
    ];

    return selectBestElement(candidates, ["peso", "weight", "targeted", "gram"], excludePatterns, {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    }) || selectBestElement(candidates, [], excludePatterns, {
      textExtractor: collectScopedFieldText,
    });
  }

  async function setStepTargetedWeightField(step, root = findStepEditorRoot()) {
    if (step?.targetedWeight == null) {
      return true;
    }

    const weightField = findStepTargetedWeightField(root);
    if (!weightField) {
      return false;
    }

    const hadDisabled = weightField.hasAttribute("disabled");
    const hadReadonly = weightField.hasAttribute("readonly");
    const previousTabIndex = weightField.getAttribute("tabindex");
    if (hadDisabled) {
      weightField.removeAttribute("disabled");
    }
    if (hadReadonly) {
      weightField.removeAttribute("readonly");
    }
    weightField.setAttribute("tabindex", "0");

    const normalizedWeight = String(step.targetedWeight);
    await commitTextInputLikeUser(weightField, normalizedWeight);
    await ensureFieldValue(weightField, normalizedWeight, "Peso target");

    if (hadReadonly) {
      weightField.setAttribute("readonly", "readonly");
    }
    if (hadDisabled) {
      weightField.setAttribute("disabled", "disabled");
    }
    if (previousTabIndex == null) {
      weightField.removeAttribute("tabindex");
    } else {
      weightField.setAttribute("tabindex", previousTabIndex);
    }

    return true;
  }

  function findStepRotationButton(root, reverse) {
    const expectedValue = reverse ? "0" : "1";
    const buttonByValue = visibleElementsWithin(root, `button[value='${expectedValue}'], [role='radio'][value='${expectedValue}']`)[0] || null;
    if (buttonByValue) {
      return buttonByValue;
    }

    const directionPattern = reverse
      ? /\b(?:antiorario|reverse|counter(?:clockwise)?)\b/
      : /\b(?:orario|clockwise)\b/;

    return uniqueElements(
      visibleElementsWithin(root, "label, button, [role='button'], [role='radio'], .v-input--selection-controls, .v-selection-control, .v-radio")
        .map((element) => element.closest("label, button, [role='button'], [role='radio'], .v-input--selection-controls, .v-selection-control, .v-radio") || element)
    ).find((element) => directionPattern.test(normalizeText([
      element.textContent,
      collectFieldText(element),
      element.className,
      element.querySelector?.("i")?.className,
    ].filter(Boolean).join(" ")).toLowerCase())) || null;
  }

  function findStepRotationInput(root, reverse) {
    const directionPattern = reverse
      ? /antiorario|reverse|counter|clockwise-?left|rotate-left|mdi-rotate-left|mdi-undo/
      : /orario|clockwise|clockwise-?right|rotate-right|mdi-rotate-right|mdi-redo/;
    const expectedValuePattern = reverse
      ? /^(?:0|false|reverse|counter|antiorario)$/
      : /^(?:1|true|clockwise|orario)$/;

    return Array.from(root.querySelectorAll("input[type='radio'], input[type='checkbox']"))
      .map((element) => ({
        element,
        signature: normalizeText([
          element.getAttribute("name"),
          element.getAttribute("id"),
          element.getAttribute("value"),
          element.getAttribute("aria-label"),
          collectScopedFieldText(element),
          element.closest("label, .v-radio, .v-selection-control, .v-input--selection-controls, .v-input")?.textContent,
          element.closest("label, .v-radio, .v-selection-control, .v-input--selection-controls, .v-input")?.className,
        ].filter(Boolean).join(" ")).toLowerCase(),
      }))
      .filter((candidate) => /reverse|clock|direction|rotation|rotazione|senso|orario|antiorario|counter|mdi-rotate|mdi-undo|mdi-redo/.test(candidate.signature))
      .sort((left, right) => Number(expectedValuePattern.test(right.signature)) - Number(expectedValuePattern.test(left.signature)) || Number(directionPattern.test(right.signature)) - Number(directionPattern.test(left.signature)))
      .find((candidate) => expectedValuePattern.test(candidate.signature) || directionPattern.test(candidate.signature))?.element || null;
  }

  function findStepReverseToggle(root = findStepEditorRoot()) {
    return visibleElementsWithin(root, "input[name*='reverse'], input[type='checkbox'][name*='clock'], input[id*='reverse'], input[id*='clock']")[0] || null;
  }

  function readCheckedState(input) {
    if (!input) {
      return false;
    }

    return Boolean(input.checked || input.getAttribute("aria-checked") === "true");
  }

  async function ensureCheckedState(input, expectedChecked) {
    if (!input) {
      return false;
    }

    const desiredState = Boolean(expectedChecked);
    if (readCheckedState(input) === desiredState) {
      return true;
    }

    const fallbackTarget = input.id && window.CSS?.escape
      ? document.querySelector(`label[for="${window.CSS.escape(input.id)}"]`)
      : null;
    const clickableTarget = input.closest("label, .v-radio, .v-selection-control, .v-input--selection-controls, .v-input, [role='radio'], [role='button']") || fallbackTarget || input;
    clickElementRobust(clickableTarget);
    await sleep(250);
    if (readCheckedState(input) === desiredState) {
      return true;
    }

    const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(input), "checked")
      || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked");
    if (descriptor?.set) {
      descriptor.set.call(input, desiredState);
    } else {
      input.checked = desiredState;
    }
    input.setAttribute("aria-checked", desiredState ? "true" : "false");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, composed: true }));
    await sleep(250);

    return readCheckedState(input) === desiredState;
  }

  function findStepSpeedField(root = findStepEditorRoot()) {
    return selectBestElement([
      ...visibleElementsWithin(root, "select[name*='speed' i]"),
      ...visibleElementsWithin(root, "input[name*='speed' i]"),
      ...visibleElementsWithin(root, "input[placeholder*='veloc' i]"),
      ...visibleElementsWithin(root, "input[aria-label*='veloc' i]"),
      ...visibleElementsWithin(root, "input[readonly][autocomplete='off']"),
      ...visibleElementsWithin(root, "input[readonly][disabled]"),
      ...visibleElementsWithin(root, "input[disabled][readonly]"),
      ...visibleElementsWithin(root, "input[readonly]"),
      ...visibleElementsWithin(root, "[role='combobox'] input"),
    ], ["veloc", "speed"], ["programma", "program", "temperatura", "temp", "tempo", "durata"], {
      textExtractor: collectScopedFieldText,
      requireIncludeMatch: true,
    });
  }

  function findStepSpeedSlider(root = findStepEditorRoot()) {
    return findStepParameterSlider(root, "speed");
  }

  async function setStepSpeedField(step, root = findStepEditorRoot()) {
    const normalizedSpeed = parseStepSpeedValue(step?.speed);
    if (!normalizedSpeed) {
      return false;
    }

    const speedField = findStepSpeedField(root);
    if (speedField) {
      const speedLabels = buildSpeedOptionLabels(normalizedSpeed);
      if (selectNativeOptionByLabels(speedField, speedLabels)) {
        return true;
      }

      const looksSelectLike = speedField.matches?.("select")
        || speedField.hasAttribute("readonly")
        || speedField.hasAttribute("disabled")
        || Boolean(speedField.closest("[role='combobox'], .v-select, .v-autocomplete"));

      if (looksSelectLike) {
        return await setSelectLikeFieldOption(speedField, speedLabels);
      }

      const hadDisabled = speedField.hasAttribute("disabled");
      const hadReadonly = speedField.hasAttribute("readonly");
      const previousTabIndex = speedField.getAttribute("tabindex");

      if (hadDisabled) {
        speedField.removeAttribute("disabled");
      }
      if (hadReadonly) {
        speedField.removeAttribute("readonly");
      }
      speedField.setAttribute("tabindex", "0");

      await commitTextInputLikeUser(speedField, normalizedSpeed);
      await ensureFieldValue(speedField, normalizedSpeed, "Velocita");

      if (hadReadonly) {
        speedField.setAttribute("readonly", "readonly");
      }
      if (hadDisabled) {
        speedField.setAttribute("disabled", "disabled");
      }
      if (previousTabIndex == null) {
        speedField.removeAttribute("tabindex");
      } else {
        speedField.setAttribute("tabindex", previousTabIndex);
      }
      return true;
    }

    const speedSlider = findStepSpeedSlider(root);
    if (!speedSlider) {
      return false;
    }

    for (const sliderTarget of resolveSpeedSliderTargets(normalizedSpeed)) {
      if (await setSliderToIndex(speedSlider, sliderTarget)) {
        return true;
      }
    }

    return false;
  }

  async function ensureStepProgramSelection(step, index) {
    if (!stepHasTechnicalPayload(step)) {
      return false;
    }

    const root = findStepEditorRoot();
    let programField = findStepProgramField(root);
    if (!programField) {
      const switchInput = findStepProgramSwitchInput(root);
      if (switchInput) {
        const switchEnabled = await ensureSwitchState(switchInput, true);
        if (!switchEnabled) {
          throw new Error(`Interruttore programma non attivabile per lo step ${index + 1}. Campi visibili:\n${debugVisibleFields()}`);
        }
      } else {
        const activator = findStepProgramActivator(root);
        if (!activator) {
          throw new Error(`Selettore programma non trovato per lo step ${index + 1}. Campi visibili:\n${debugVisibleFields()}`);
        }

        clickElementRobust(activator);
      }

      const fieldDeadline = Date.now() + 4000;
      while (Date.now() < fieldDeadline) {
        await sleep(150);
        programField = findStepProgramField(root);
        if (programField) {
          break;
        }
      }
    }

    if (!programField) {
      const activator = findStepProgramActivator(root);
      if (!activator) {
        throw new Error(`Selettore programma non trovato per lo step ${index + 1}. Campi visibili:\n${debugVisibleFields()}`);
      }
      clickElementRobust(activator);
      await sleep(250);
      programField = findStepProgramField(root);
      if (!programField) {
        throw new Error(`Campo programma non trovato per lo step ${index + 1}. Campi visibili:\n${debugVisibleFields()}`);
      }
    }

    const selectedProgram = step.selectedProgram || CUSTOM_COOKING_PROGRAM_LABEL;

    if (await setSelectLikeFieldOption(programField, [selectedProgram])) {
      if (normalizeText(selectedProgram).toLowerCase() === CUSTOM_COOKING_PROGRAM_LABEL.toLowerCase()) {
        await sleep(350);
        await waitForCustomCookingControls(step, 9000);
      }
      return true;
    }

    throw new Error(`Opzione programma '${selectedProgram}' non trovata per lo step ${index + 1}. Bottoni visibili:\n${debugVisibleButtons()}`);
  }

  async function setStepReverseDirection(step, root = findStepEditorRoot()) {
    if (!step || step.isDescriptive) {
      return false;
    }

    const rotationInput = findStepRotationInput(root, Boolean(step.reverse));
    if (rotationInput) {
      const targetState = rotationInput.type === "checkbox" ? Boolean(step.reverse) : true;
      if (await ensureCheckedState(rotationInput, targetState)) {
        return true;
      }
    }

    const rotationButton = findStepRotationButton(root, Boolean(step.reverse));
    if (rotationButton) {
      clickElementRobust(rotationButton);
      await sleep(150);
      return true;
    }

    const directToggle = findStepReverseToggle(root);
    if (directToggle) {
      const shouldBeChecked = Boolean(step.reverse);
      if (Boolean(directToggle.checked) !== shouldBeChecked) {
        clickElementRobust(directToggle.closest("label, .v-input--selection-controls, .v-selection-control, .v-input") || directToggle);
        await sleep(150);
      }
      return Boolean(directToggle.checked) === shouldBeChecked || !shouldBeChecked;
    }

    if (!step.reverse) {
      return true;
    }

    const reverseCandidate = uniqueElements(
      visibleElementsWithin(root, "label, button, [role='button'], [role='radio'], .v-input--selection-controls, .v-selection-control, .v-radio")
        .map((element) => element.closest("label, button, [role='button'], [role='radio'], .v-input--selection-controls, .v-selection-control, .v-radio") || element)
    ).find((element) => /antiorario|counter|reverse/.test(normalizeText(element.textContent || collectFieldText(element)).toLowerCase()));

    if (reverseCandidate) {
      clickElementRobust(reverseCandidate);
      await sleep(150);
      return true;
    }

    return false;
  }

  function hasRequiredCustomCookingControls(step, root) {
    return describeMissingCustomCookingControls(step, root).length === 0;
  }

  function hasRequiredScaleControls(step, root) {
    return step?.targetedWeight == null || Boolean(findStepTargetedWeightField(root));
  }

  function describeMissingCustomCookingControls(step, root) {
    const requiresTemperature = step?.temperatureC != null;
    const requiresDuration = step?.durationSeconds != null;
    const requiresSpeed = Boolean(normalizeText(step?.speed || ""));
    const requiresReverse = Boolean(step?.reverse);
    const { directDurationField, hoursField, minutesField } = findStepDurationFields(root);
    const missing = [];

    const durationReady = !requiresDuration || Boolean(
      directDurationField
      || (hoursField && minutesField)
      || minutesField
      || hoursField
    );
    const temperatureReady = !requiresTemperature || Boolean(findStepTemperatureInput(root) || findStepTemperatureSlider(root));
    const speedReady = !requiresSpeed || Boolean(findStepSpeedField(root) || findStepSpeedSlider(root));
    const reverseReady = !requiresReverse || Boolean(
      findStepRotationInput(root, true)
      || findStepRotationButton(root, true)
      || findStepReverseToggle(root)
    );

    if (!temperatureReady) {
      missing.push("temperatura");
    }
    if (!durationReady) {
      missing.push("tempo");
    }
    if (!speedReady) {
      missing.push("velocita");
    }
    if (!reverseReady) {
      missing.push("senso di rotazione");
    }

    return missing;
  }

  async function waitForCustomCookingControls(step, timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const root = findStepEditorRoot();
      if (hasRequiredCustomCookingControls(step, root)) {
        return root;
      }
      await sleep(150);
    }

    return findStepEditorRoot();
  }

  async function waitForScaleControls(step, timeoutMs = 5000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const root = findStepEditorRoot();
      if (hasRequiredScaleControls(step, root)) {
        return root;
      }
      await sleep(150);
    }

    return findStepEditorRoot();
  }

  async function recoverCustomCookingControls(step, programField) {
    const selectedProgram = step?.selectedProgram || CUSTOM_COOKING_PROGRAM_LABEL;

    for (let attempt = 0; attempt < 3; attempt += 1) {
      const root = await waitForCustomCookingControls(step, 2500 + (attempt * 1500));
      if (hasRequiredCustomCookingControls(step, root)) {
        return root;
      }

      const liveProgramField = findStepProgramField(findStepEditorRoot()) || programField;
      if (liveProgramField) {
        await settleSelectLikeField(liveProgramField);
      }

      const settledRoot = findStepEditorRoot();
      if (hasRequiredCustomCookingControls(step, settledRoot)) {
        return settledRoot;
      }

      if (liveProgramField && await setSelectLikeFieldOption(liveProgramField, [selectedProgram])) {
        await sleep(500);
        const refreshedRoot = findStepEditorRoot();
        if (hasRequiredCustomCookingControls(step, refreshedRoot)) {
          return refreshedRoot;
        }
      }
    }

    const switchInput = findStepProgramSwitchInput(findStepEditorRoot());
    if (switchInput && readSwitchCheckedState(switchInput)) {
      const switchDisabled = await ensureSwitchState(switchInput, false);
      if (switchDisabled) {
        await sleep(450);
        const switchedRoot = findStepEditorRoot();
        if (hasRequiredCustomCookingControls(step, switchedRoot)) {
          return switchedRoot;
        }
      }

      await ensureSwitchState(switchInput, true);
      await sleep(300);

      const restoredProgramField = findStepProgramField(findStepEditorRoot()) || programField;
      if (restoredProgramField && await setSelectLikeFieldOption(restoredProgramField, [selectedProgram])) {
        await sleep(500);
        const restoredRoot = findStepEditorRoot();
        if (hasRequiredCustomCookingControls(step, restoredRoot)) {
          return restoredRoot;
        }
      }
    }

    return findStepEditorRoot();
  }

  async function tryApplyCustomCookingConfiguration(step, root) {
    const failures = [];

    if (step.temperatureC != null && !await setStepTemperatureField(step, root)) {
      failures.push("temperatura");
    }
    if (step.durationSeconds != null && !await setStepDurationFields(step, root)) {
      failures.push("tempo");
    }
    if (normalizeText(step.speed || "") && !await setStepSpeedField(step, root)) {
      failures.push("velocita");
    }
    if (!await setStepReverseDirection(step, root) && step.reverse) {
      failures.push("senso di rotazione");
    }

    return failures;
  }

  async function applyCustomCookingConfiguration(step, index) {
    let root = await waitForCustomCookingControls(step, 15000);
    let failures = await tryApplyCustomCookingConfiguration(step, root);

    if (failures.length) {
      await sleep(1500);
      root = await waitForCustomCookingControls(step, 7000);
      failures = await tryApplyCustomCookingConfiguration(step, root);
    }

    if (failures.length) {
      throw new Error(`Impossibile impostare ${failures.join(", ")} per lo step ${index + 1}.\n\nDiagnostica controlli custom:\n${debugCustomCookingControls(root)}\n\nCampi visibili:\n${debugVisibleFields(12)}\n\nBottoni visibili:\n${debugVisibleButtons(16)}`);
    }

    return true;
  }

  async function applyScaleConfiguration(step, index) {
    const root = await waitForScaleControls(step, 8000);
    if (await setStepTargetedWeightField(step, root)) {
      return true;
    }

    throw new Error(`Impossibile impostare il peso target per lo step ${index + 1}.\n\nDiagnostica controlli bilancia:\n${debugScaleControls(root)}\n\nCampi visibili:\n${debugVisibleFields(12)}\n\nBottoni visibili:\n${debugVisibleButtons(16)}`);
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

    if (stepHasTechnicalPayload(step)) {
      await ensureStepProgramSelection(step, index);
    }

    if (!step.isTechnicalOnly) {
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
    }

    if (stepHasTechnicalPayload(step) && isScaleProgram(step)) {
      await applyScaleConfiguration(step, index);
    }

    if (stepHasTechnicalPayload(step) && isCustomCookingProgram(step)) {
      await applyCustomCookingConfiguration(step, index);
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

    const showCopyableError = (message) => {
      const normalizedMessage = String(message || "Errore sconosciuto");
      try {
        window.prompt("Compilazione non riuscita. Copia il messaggio:", normalizedMessage);
      } catch (promptError) {
        log("Prompt errore non disponibile", promptError);
        window.alert(`Compilazione non riuscita: ${normalizedMessage}`);
      }
    };

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
            log("Compilazione non riuscita", error);
            showCopyableError(error?.message || error);
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
