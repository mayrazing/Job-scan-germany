(() => {
  const requestOptions = (action, form) => {
    const options = {
      method: "POST",
      credentials: "same-origin",
    };
    if (
      action === "restore"
      || action === "snapshot"
      || action === "snapshot-force"
      || action === "re-evaluate"
    ) {
      return options;
    }
    if (action === "resume") {
      return {
        ...options,
        body: new FormData(form),
      };
    }
    if (action === "salary") {
      return {
        ...options,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_salary: form.elements.namedItem("expected_salary").value,
          expected_salary_period: form.elements.namedItem("expected_salary_period").value,
          offer_salary: form.elements.namedItem("offer_salary").value,
          offer_salary_period: form.elements.namedItem("offer_salary_period").value,
        }),
      };
    }
    const status = form.elements.namedItem("status").value;
    return {
      ...options,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    };
  };

  const sourceFilterKey = "job-scan.review-source-filter.v1";
  const globalSourceFilterKey = "job-scan.global-source-filter.v1";
  let jobBatchController = null;
  let userTagController = null;

  const cardSources = (card) => card.dataset.sources.split(",").filter(Boolean);
  const cardUserTags = (card) => [
    ...card.querySelectorAll("[data-user-tag-name]"),
  ].map((tag) => tag.dataset.userTagName);

  const sourceLabels = {
    arbeitsagentur: "Arbeitsagentur",
    bosch: "Bosch",
    dallmeier: "Dallmeier",
    dhl: "DHL",
    glassdoor: "Glassdoor",
    indeed: "Indeed",
    linkedin: "LinkedIn",
    manual: "Manual",
    siemens: "Siemens",
    simplify: "Simplify",
    stepstone: "StepStone",
    successfactors: "Rohde & Schwarz",
    telekom: "Deutsche Telekom",
    thyssenkrupp: "thyssenkrupp",
  };

  const restoredSourceFilter = (sources, storageKey = sourceFilterKey) => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey));
      const sameSources =
        Array.isArray(saved?.sources) &&
        saved.sources.length === sources.length &&
        saved.sources.every((source, index) => source === sources[index]);
      if (!sameSources || !Array.isArray(saved.selected)) return sources;
      return saved.selected.filter((source) => sources.includes(source));
    } catch (_error) {
      return sources;
    }
  };

  const saveSourceFilter = (sources, selected, storageKey = sourceFilterKey) => {
    try {
      window.localStorage.setItem(
        storageKey,
        JSON.stringify({ sources, selected }),
      );
    } catch (_error) {
      // Filtering remains usable when browser storage is disabled or full.
    }
  };

  const reviewFilterStateKey = "job-scan.review-filter-state.v1";

  const restoredReviewFilterState = () => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(reviewFilterStateKey));
      return saved && typeof saved === "object" ? saved : {};
    } catch (_error) {
      return {};
    }
  };

  const saveReviewFilterState = (patch) => {
    try {
      window.localStorage.setItem(
        reviewFilterStateKey,
        JSON.stringify({ ...restoredReviewFilterState(), ...patch }),
      );
    } catch (_error) {
      // Filtering remains usable when browser storage is disabled or full.
    }
  };

  const postedWithinWindow = (card, days) => {
    if (days === "") return true;
    if (!card.dataset.postedAt) return false;
    const cutoff = new Date();
    cutoff.setHours(0, 0, 0, 0);
    cutoff.setDate(cutoff.getDate() - Number(days));
    return new Date(`${card.dataset.postedAt}T00:00:00`) >= cutoff;
  };

  const matchesCompanySize = (card, minimum) => {
    if (minimum === "0") return true;
    if (!card.dataset.companySizeMinimum) return false;
    const maximum = card.dataset.companySizeMaximum;
    return maximum === "" || Number(maximum) >= Number(minimum);
  };

  const cardIndustry = (card) => card.dataset.companyIndustry || "";

  const matchesCompanyIndustry = (card, selectedIndustry) => {
    if (selectedIndustry === "") return true;
    if (selectedIndustry === "unknown") return cardIndustry(card) === "";
    return selectedIndustry === `known:${cardIndustry(card)}`;
  };

  const matchesMinimumScore = (card, minimum) => {
    if (minimum === "") return true;
    return card.dataset.score !== "" && Number(card.dataset.score) >= Number(minimum);
  };

  const matchesLanguageRequirement = (card, selectedRequirement) => {
    if (selectedRequirement === "") return true;
    if (selectedRequirement === "required") {
      return card.dataset.germanRequirement === "required";
    }
    return ["none", "optional"].includes(card.dataset.germanRequirement);
  };

  const trackingQueryKeys = new Set([
    "fbclid",
    "gclid",
    "ref",
    "referrer",
    "source",
  ]);

  const normalizedJobUrl = (value) => {
    const normalized = value.trim().toLowerCase();
    if (normalized === "") return "";
    try {
      const url = new URL(normalized);
      url.hash = "";
      [...url.searchParams.keys()].forEach((key) => {
        if (key.startsWith("utm_") || trackingQueryKeys.has(key)) {
          url.searchParams.delete(key);
        }
      });
      url.searchParams.sort();
      url.pathname = url.pathname.replace(/\/+$/, "") || "/";
      return url.toString();
    } catch (_error) {
      return normalized;
    }
  };

  const matchesJobUrl = (card, query) => {
    const normalizedQuery = normalizedJobUrl(query);
    if (normalizedQuery === "") return true;
    return normalizedJobUrl(card.dataset.jobUrl || "").includes(normalizedQuery);
  };

  const updateReviewGroupCounts = () => {
    document.querySelectorAll("[data-review-workspace]").forEach((workspace) => {
      workspace.querySelectorAll(".review-groups > .job-group").forEach((group) => {
        const cards = [...group.querySelectorAll(".job-card")];
        const visibleCount = cards.filter(
          (card) => !card.hidden,
        ).length;
        workspace
          .querySelectorAll(`[data-review-group-count="${group.id}"]`)
          .forEach((count) => {
            count.textContent = String(visibleCount);
          });
        if (workspace.closest('[data-review-block="global"]')) {
          const settingsRow = document.querySelector(
            `[data-tracker-group-row][data-group-id="${CSS.escape(group.id)}"]`,
          );
          if (settingsRow) {
            settingsRow.dataset.groupCount = String(cards.length);
            settingsRow.querySelector(".tracker-group-job-count").textContent =
              `${cards.length} ${cards.length === 1 ? "job" : "jobs"}`;
          }
        }
      });
    });
  };

  const updateReviewGroupNoticeCounts = () => {
    document.querySelectorAll("[data-review-workspace]").forEach((workspace) => {
      workspace.querySelectorAll(".review-groups > .job-group").forEach((group) => {
        const count = group.querySelectorAll(
          ':scope > .card-grid > .job-card[data-reevaluation-status="succeeded"], '
          + ':scope > .card-grid > .job-card[data-reevaluation-status="failed"]',
        ).length;
        workspace
          .querySelectorAll(`[data-review-group-notice-count="${group.id}"]`)
          .forEach((badge) => {
            badge.textContent = String(count);
            badge.hidden = count === 0;
            badge.setAttribute(
              "aria-label",
              `${count} unacknowledged re-evaluation ${count === 1 ? "result" : "results"}`,
            );
          });
      });
    });
  };

  const applyReviewFilters = (
    cards,
    values,
    postedWithinDays,
    minimumScore,
    companySizeMinimum,
    companyIndustry,
    languageRequirement,
    jobUrl = "",
    userTags = [],
  ) => {
    const selected = new Set(values);
    const selectedTags = new Set(userTags);
    cards.forEach((card) => {
      const matchesSource = cardSources(card).some((source) => selected.has(source));
      const matchesTag = selectedTags.size === 0 || cardUserTags(card).some(
        (tag) => selectedTags.has(tag),
      );
      card.hidden =
        !matchesSource ||
        !matchesTag ||
        !postedWithinWindow(card, postedWithinDays) ||
        !matchesMinimumScore(card, minimumScore) ||
        !matchesCompanySize(card, companySizeMinimum) ||
        !matchesCompanyIndustry(card, companyIndustry) ||
        !matchesLanguageRequirement(card, languageRequirement) ||
        !matchesJobUrl(card, jobUrl);
    });
    updateReviewGroupCounts();
  };

  const initializeSourceFilter = () => {
    const select = document.querySelector("#source-filter");
    if (!select) return;
    const currentCards = () => [
      ...document.querySelectorAll(
        '[data-review-block="current"] .review-groups [data-sources]',
      ),
    ];
    const postedWithinSelect = document.querySelector("#review-posted-within-days");
    const minimumScoreSelect = document.querySelector("#review-minimum-score");
    const companySizeSelect = document.querySelector("#review-company-size");
    const companyIndustrySelect = document.querySelector("#review-company-industry");
    const languageRequirementSelect = document.querySelector(
      "#review-language-requirement",
    );
    const cards = currentCards();
    const sources = [...new Set(cards.flatMap(cardSources))].sort();
    if (sources.length === 0) {
      select.closest(".source-filter").hidden = true;
      return;
    }

    const selectedSources = new Set(restoredSourceFilter(sources));
    sources.forEach((source) => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = sourceLabels[source] || source;
      option.selected = selectedSources.has(source);
      select.append(option);
    });
    const industries = [...new Set(cards.map(cardIndustry).filter(Boolean))].sort(
      (left, right) => left.localeCompare(right),
    );
    industries.forEach((industry) => {
      const option = document.createElement("option");
      option.value = `known:${industry}`;
      option.textContent = industry;
      companyIndustrySelect?.append(option);
    });
    if (cards.some((card) => cardIndustry(card) === "")) {
      const option = document.createElement("option");
      option.value = "unknown";
      option.textContent = "Unknown";
      companyIndustrySelect?.append(option);
    }

    const reviewFilterState = restoredReviewFilterState();
    const restoreSelectValue = (select, value) => {
      if (!select || value === undefined) return;
      if ([...select.options].some((option) => option.value === value)) {
        select.value = value;
      }
    };
    restoreSelectValue(postedWithinSelect, reviewFilterState.postedWithinDays);
    restoreSelectValue(minimumScoreSelect, reviewFilterState.minimumScore);
    restoreSelectValue(companySizeSelect, reviewFilterState.companySize);
    restoreSelectValue(companyIndustrySelect, reviewFilterState.companyIndustry);
    restoreSelectValue(languageRequirementSelect, reviewFilterState.languageRequirement);

    const control = new TomSelect(select, {
      plugins: {
        checkbox_options: {},
      },
      closeAfterSelect: false,
      hideSelected: false,
      maxItems: null,
      onChange(values) {
        const selected = Array.isArray(values) ? values : [values].filter(Boolean);
        saveSourceFilter(sources, selected);
        applyReviewFilters(
          currentCards(),
          selected,
          postedWithinSelect?.value ?? "",
          minimumScoreSelect?.value ?? "",
          companySizeSelect?.value ?? "0",
          companyIndustrySelect?.value ?? "",
          languageRequirementSelect?.value ?? "",
        );
      },
    });
    const updateSourceSummary = () => {
      const count = control.items.length;
      control.control.dataset.summary = `${count} source${count === 1 ? "" : "s"} selected`;
    };
    control.on("change", updateSourceSummary);
    control.control.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        control.focus();
        control.refreshOptions(false);
        control.open();
      },
      true,
    );
    updateSourceSummary();
    const applyCurrentFilters = () => {
      applyReviewFilters(
        currentCards(),
        control.items,
        postedWithinSelect?.value ?? "",
        minimumScoreSelect?.value ?? "",
        companySizeSelect?.value ?? "0",
        companyIndustrySelect?.value ?? "",
        languageRequirementSelect?.value ?? "",
      );
    };
    applyCurrentFilters();
    const handleFilterSelectChange = (select, field) => {
      select?.addEventListener("change", () => {
        saveReviewFilterState({ [field]: select.value });
        applyCurrentFilters();
      });
    };
    handleFilterSelectChange(postedWithinSelect, "postedWithinDays");
    handleFilterSelectChange(minimumScoreSelect, "minimumScore");
    handleFilterSelectChange(companySizeSelect, "companySize");
    handleFilterSelectChange(companyIndustrySelect, "companyIndustry");
    handleFilterSelectChange(languageRequirementSelect, "languageRequirement");
    document.addEventListener("job-scan:review-updated", applyCurrentFilters);
  };

  const initializeUserTags = () => {
    const filter = document.querySelector("#global-tag-filter");
    const globalBlock = document.querySelector('[data-review-block="global"]');
    if (!filter || !globalBlock) return null;

    let tagDefinitions = new Map();
    let filterControl = null;
    const cardControls = new Set();
    const normalizedTagName = (name) => name.trim().toLocaleLowerCase();
    const activeGroup = () => {
      const groupId = globalBlock.querySelector(
        '[data-review-group-tab][aria-current="page"]',
      )?.dataset.reviewGroupTab;
      return groupId
        ? globalBlock.querySelector(
          `.review-groups > .job-group#${CSS.escape(groupId)}`,
        )
        : null;
    };
    const activeCards = () => [
      ...activeGroup()?.querySelectorAll(":scope > .card-grid > .job-card") ?? [],
    ];
    const allCards = () => [
      ...globalBlock.querySelectorAll(".review-groups .job-card"),
    ];
    const definitionsFor = (cards) => {
      const definitions = new Map();
      cards.forEach((card) => {
        card.querySelectorAll("[data-user-tag-name]").forEach((tag) => {
          const name = tag.dataset.userTagName;
          if (!name) return;
          definitions.set(normalizedTagName(name), {
            name,
            color: tag.dataset.userTagColor || "#2F6F5E",
          });
        });
      });
      return new Map(
        [...definitions.entries()].sort((left, right) => (
          left[1].name.localeCompare(right[1].name)
        )),
      );
    };
    const replaceOptions = (control, definitions, selected = []) => {
      const availableNames = new Set(
        [...definitions.values()].map((tag) => tag.name),
      );
      const retained = selected.filter((name) => availableNames.has(name));
      control.clear(true);
      control.clearOptions();
      definitions.forEach((tag) => {
        control.addOption({
          value: tag.name,
          text: tag.name,
          color: tag.color,
        });
      });
      retained.forEach((name) => control.addItem(name, true));
      control.refreshOptions(false);
    };
    const updateFilterPlaceholder = () => {
      if (!filterControl) return;
      filterControl.control_input.placeholder = filterControl.items.length
        ? ""
        : "Filter by tag";
    };
    const initializeCardSelect = (select) => {
      if (select.tomselect) return select.tomselect;
      const colorInput = select.form?.elements.namedItem("color");
      const control = new TomSelect(select, {
        create: true,
        createOnBlur: true,
        dropdownParent: "body",
        persist: false,
        maxItems: 1,
        closeAfterSelect: true,
        placeholder: "input tag",
        render: {
          option(data, escape) {
            const color = data.color || "#2F6F5E";
            return `
              <div class="job-user-tag-option">
                <span
                  class="job-user-tag-option-color"
                  data-user-tag-option-color="${escape(color)}"
                  style="--tag-color: ${escape(color)}"
                ></span>
                <span class="job-user-tag-option-name">${escape(data.text)}</span>
              </div>
            `;
          },
        },
        onChange(value) {
          const definition = tagDefinitions.get(normalizedTagName(value || ""));
          if (definition && colorInput) colorInput.value = definition.color;
        },
      });
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "job-user-tag-toggle";
      toggle.dataset.userTagToggle = "";
      toggle.setAttribute("aria-expanded", "false");
      toggle.setAttribute("aria-haspopup", "listbox");
      toggle.setAttribute("aria-label", "Show tag options");
      toggle.title = "Show tag options";
      toggle.addEventListener("pointerdown", (event) => {
        event.preventDefault();
        event.stopPropagation();
      });
      toggle.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (control.isOpen) {
          control.close();
        } else {
          control.focus();
          control.open();
        }
      });
      control.on("dropdown_open", () => {
        toggle.setAttribute("aria-expanded", "true");
      });
      control.on("dropdown_close", () => {
        toggle.setAttribute("aria-expanded", "false");
      });
      const scrollContainer = select.closest(".review-groups");
      const repositionDropdown = () => {
        if (control.isOpen) control.positionDropdown();
      };
      scrollContainer?.addEventListener("scroll", repositionDropdown, {
        passive: true,
      });
      control.on("destroy", () => {
        scrollContainer?.removeEventListener("scroll", repositionDropdown);
      });
      control.wrapper.append(toggle);
      cardControls.add(control);
      return control;
    };
    const sync = () => {
      cardControls.forEach((control) => {
        if (control.input.isConnected) return;
        control.destroy();
        cardControls.delete(control);
      });
      const cards = activeCards();
      tagDefinitions = definitionsFor(allCards());
      replaceOptions(filterControl, tagDefinitions, filterControl.items);
      cards.forEach((card) => {
        const select = card.querySelector("[data-user-tag-select]");
        if (!select) return;
        const control = initializeCardSelect(select);
        replaceOptions(control, tagDefinitions);
      });
    };
    const addChip = (list, tag) => {
      list.querySelector("[data-user-tag-empty]")?.remove();
      const existing = [...list.querySelectorAll("[data-user-tag-name]")].find(
        (chip) => normalizedTagName(chip.dataset.userTagName) === normalizedTagName(tag.name),
      );
      if (existing) return;
      const chip = document.createElement("span");
      chip.className = "job-user-tag";
      chip.dataset.userTagName = tag.name;
      chip.dataset.userTagColor = tag.color;
      chip.style.setProperty("--tag-color", tag.color);
      const label = document.createElement("span");
      label.textContent = tag.name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.deleteUserTag = "";
      remove.ariaLabel = `Remove ${tag.name}`;
      remove.title = `Remove ${tag.name}`;
      remove.textContent = "×";
      chip.append(label, remove);
      list.append(chip);
    };
    const showEmptyTagList = (list) => {
      if (list.querySelector("[data-user-tag-name]")) return;
      const empty = document.createElement("span");
      empty.className = "job-tag-empty";
      empty.dataset.userTagEmpty = "";
      empty.textContent = "No tags yet";
      list.append(empty);
    };

    filterControl = new TomSelect(filter, {
      plugins: {
        input_autogrow: {},
        remove_button: { title: "Remove" },
      },
      closeAfterSelect: false,
      hideSelected: true,
      maxItems: null,
      placeholder: "Filter by tag",
      onChange() {
        document.dispatchEvent(new CustomEvent("job-scan:tag-filter-changed"));
      },
    });
    filterControl.on("change", updateFilterPlaceholder);

    document.addEventListener("submit", async (event) => {
      const form = event.target.closest("[data-user-tag-form]");
      if (!form) return;
      event.preventDefault();
      const select = form.querySelector("[data-user-tag-select]");
      const control = select?.tomselect;
      const name = String(control?.getValue() || "").trim();
      const color = form.elements.namedItem("color")?.value;
      const button = form.querySelector('button[type="submit"]');
      const errorMessage = form.querySelector("[data-user-tag-error]");
      if (!name || !color || !button || !errorMessage) return;
      button.disabled = true;
      errorMessage.hidden = true;
      try {
        const response = await fetch(
          `/api/global-jobs/${encodeURIComponent(form.dataset.userTagJobKey)}/tags`,
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name, color }),
          },
        );
        if (!response.ok) {
          throw new Error(await responseError(response, "Could not save this tag."));
        }
        const tag = await response.json();
        addChip(form.closest("[data-job-tag-panel]").querySelector("[data-user-tag-list]"), tag);
        control.clear(true);
        sync();
        document.dispatchEvent(new CustomEvent("job-scan:tag-filter-changed"));
      } catch (error) {
        errorMessage.textContent = error.message;
        errorMessage.hidden = false;
      } finally {
        button.disabled = false;
      }
    });

    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-delete-user-tag]");
      if (!button) return;
      const chip = button.closest("[data-user-tag-name]");
      const panel = button.closest("[data-job-tag-panel]");
      const card = button.closest("[data-job-key]");
      const errorMessage = panel?.querySelector("[data-user-tag-error]");
      if (!chip || !panel || !card || !errorMessage) return;
      button.disabled = true;
      errorMessage.hidden = true;
      try {
        const response = await fetch(
          `/api/global-jobs/${encodeURIComponent(card.dataset.jobKey)}/tags`,
          {
            method: "DELETE",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: chip.dataset.userTagName }),
          },
        );
        if (!response.ok) {
          throw new Error(await responseError(response, "Could not remove this tag."));
        }
        const list = chip.closest("[data-user-tag-list]");
        chip.remove();
        showEmptyTagList(list);
        sync();
        document.dispatchEvent(new CustomEvent("job-scan:tag-filter-changed"));
      } catch (error) {
        errorMessage.textContent = error.message;
        errorMessage.hidden = false;
        button.disabled = false;
      }
    });

    document.addEventListener("job-scan:review-updated", sync);
    document.addEventListener("job-scan:review-group-changed", sync);
    sync();
    return {
      selected: () => [...filterControl.items],
      sync,
    };
  };

  const initializeGlobalSourceFilter = () => {
    const select = document.querySelector("#global-source-filter");
    if (!select) return;
    const sourceFilter = select.closest(".source-filter");
    const urlFilter = document.querySelector("#global-url-filter");
    const globalCards = () => [
      ...document.querySelectorAll(
        '[data-review-block="global"] .review-groups [data-sources]',
      ),
    ];
    let sources = [...new Set(globalCards().flatMap(cardSources))].sort();

    const selectedSources = new Set(
      restoredSourceFilter(sources, globalSourceFilterKey),
    );
    sources.forEach((source) => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = sourceLabels[source] || source;
      option.selected = selectedSources.has(source);
      select.append(option);
    });

    let control = null;
    const updateSourceSummary = () => {
      if (!control) return;
      const count = control.items.length;
      control.control.dataset.summary = `${count} source${count === 1 ? "" : "s"} selected`;
    };
    const syncSourceOptions = () => {
      const nextSources = [...new Set(globalCards().flatMap(cardSources))].sort();
      const nextSourceSet = new Set(nextSources);
      sources
        .filter((source) => !nextSourceSet.has(source))
        .forEach((source) => {
          control.removeItem(source, true);
          control.removeOption(source);
        });
      const existingSourceSet = new Set(sources);
      nextSources
        .filter((source) => !existingSourceSet.has(source))
        .forEach((source) => {
          control.addOption({ value: source, text: sourceLabels[source] || source });
          control.addItem(source, true);
      });
      sources = nextSources;
      saveSourceFilter(sources, control.items, globalSourceFilterKey);
      updateSourceSummary();
    };
    const applyGlobalFilters = () => {
      syncSourceOptions();
      applyReviewFilters(
        globalCards(),
        control.items,
        "",
        "",
        "0",
        "",
        "",
        urlFilter?.value ?? "",
        userTagController?.selected() ?? [],
      );
    };
    control = new TomSelect(select, {
      plugins: {
        checkbox_options: {},
      },
      closeAfterSelect: false,
      hideSelected: false,
      maxItems: null,
      onChange(values) {
        const selected = Array.isArray(values) ? values : [values].filter(Boolean);
        saveSourceFilter(sources, selected, globalSourceFilterKey);
        applyGlobalFilters();
      },
    });
    control.on("change", updateSourceSummary);
    control.control.addEventListener(
      "click",
      (event) => {
        event.preventDefault();
        event.stopImmediatePropagation();
        control.focus();
        control.refreshOptions(false);
        control.open();
      },
      true,
    );
    updateSourceSummary();
    applyGlobalFilters();
    urlFilter?.addEventListener("input", applyGlobalFilters);
    document.addEventListener("job-scan:tag-filter-changed", applyGlobalFilters);
    document.addEventListener("job-scan:review-group-changed", applyGlobalFilters);
    document.addEventListener("job-scan:review-updated", applyGlobalFilters);
  };

  const globalSortKeyFor = (mode) => {
    if (mode === "added-desc" || mode === "added-asc") {
      return (card) =>
        card.dataset.addedAt ? new Date(card.dataset.addedAt).getTime() : null;
    }
    if (mode === "applied-desc" || mode === "applied-asc") {
      return (card) =>
        card.dataset.appliedAt ? new Date(card.dataset.appliedAt).getTime() : null;
    }
    if (mode === "posted-desc" || mode === "posted-asc") {
      return (card) =>
        card.dataset.postedAt
          ? new Date(`${card.dataset.postedAt}T00:00:00`).getTime()
          : null;
    }
    if (mode === "score-desc" || mode === "score-asc") {
      return (card) =>
        card.dataset.score === "" ? null : Number(card.dataset.score);
    }
    return null;
  };

  const updateAppliedSortAvailability = (groupId) => {
    const select = document.querySelector("#global-sort");
    if (!select) return;
    const appliedSelected = groupId === "applied";
    select.querySelectorAll('option[value^="applied-"]').forEach((option) => {
      option.hidden = !appliedSelected;
      option.disabled = !appliedSelected;
    });
    if (!appliedSelected && select.value.startsWith("applied-")) {
      select.value = "";
    }
  };

  const initializeGlobalSort = () => {
    const select = document.querySelector("#global-sort");
    if (!select) return;
    const applySort = () => {
      const mode = select.value;
      const keyFor = globalSortKeyFor(mode);
      if (!keyFor) return;
      const ascending = mode.endsWith("-asc");
      document
        .querySelectorAll(
          '[data-review-block="global"] .review-groups > .job-group > .card-grid',
        )
        .forEach((grid) => {
          const cards = [...grid.querySelectorAll(":scope > .job-card")];
          cards.sort((left, right) => {
            const leftKey = keyFor(left);
            const rightKey = keyFor(right);
            if (leftKey === null && rightKey === null) return 0;
            if (leftKey === null) return 1;
            if (rightKey === null) return -1;
            return ascending ? leftKey - rightKey : rightKey - leftKey;
          });
          cards.forEach((card) => grid.append(card));
        });
    };
    select.addEventListener("change", applySort);
    document.addEventListener("job-scan:review-updated", () => {
      if (select.value) applySort();
    });
  };

  const reviewGroupOrderKey = "job-scan.review-group-order.v1";

  const restoredReviewGroupOrder = (storageKey, availableIds) => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey));
      if (!Array.isArray(saved)) return availableIds;
      const normalized = saved.map((id) => id === "shortlisted" ? "saved" : id);
      const available = new Set(availableIds);
      const restored = normalized.filter(
        (id, index) =>
          typeof id === "string" &&
          available.has(id) &&
          normalized.indexOf(id) === index,
      );
      const order = [
        ...restored,
        ...availableIds.filter((id) => !restored.includes(id)),
      ];
      if (saved.includes("shortlisted")) {
        try {
          window.localStorage.setItem(storageKey, JSON.stringify(order));
        } catch (_error) {
          // Keep the migrated in-memory order when browser storage cannot be written.
        }
      }
      return order;
    } catch (_error) {
      return availableIds;
    }
  };

  const saveReviewGroupOrder = (storageKey, order) => {
    try {
      window.localStorage.setItem(storageKey, JSON.stringify(order));
    } catch (_error) {
      // Reordering remains usable when browser storage is disabled or full.
    }
  };

  const reviewGroupPanels = (container) => [
    ...container.querySelectorAll(":scope > section.job-group"),
  ];

  const reviewGroupTabs = (navigation) => [
    ...navigation.querySelectorAll(":scope > [data-review-group-tab]"),
  ];

  const applyReviewGroupOrder = (container, navigation, order) => {
    const panels = new Map(
      reviewGroupPanels(container).map((panel) => [panel.id, panel]),
    );
    order.forEach((id) => {
      const panel = panels.get(id);
      if (panel) container.append(panel);
    });
    const tabs = new Map(
      reviewGroupTabs(navigation).map((tab) => [
        tab.dataset.reviewGroupTab,
        tab,
      ]),
    );
    order.forEach((id) => {
      const tab = tabs.get(id);
      if (tab) navigation.append(tab);
    });
    const announcement = navigation.querySelector(".review-group-announcement");
    if (announcement) navigation.append(announcement);
  };

  const requestedReviewGroupId = (availableIds, navigation) => {
    const requestedId = window.location.hash.slice(1);
    if (["history", "history-stale", "history-closed"].includes(requestedId)) {
      return availableIds[0];
    }
    if (availableIds.includes(requestedId)) return requestedId;
    return (
      navigation.querySelector('[aria-current="page"]')?.dataset.reviewGroupTab
      ?? availableIds[0]
    );
  };

  const selectReviewGroup = (container, navigation, groupId, updateHash = false) => {
    const selectedPanel = reviewGroupPanels(container).find(
      (panel) => panel.id === groupId,
    );
    if (!selectedPanel) return;
    reviewGroupPanels(container).forEach((panel) => {
      panel.hidden = panel !== selectedPanel;
    });
    reviewGroupTabs(navigation).forEach((tab) => {
      if (tab.dataset.reviewGroupTab === groupId) {
        tab.setAttribute("aria-current", "page");
      } else {
        tab.removeAttribute("aria-current");
      }
    });
    const details = selectedPanel.matches("details")
      ? selectedPanel
      : selectedPanel.querySelector(":scope > details");
    if (details) details.open = true;
    if (navigation.closest('[data-review-block="global"]')) {
      updateAppliedSortAvailability(groupId);
      document.dispatchEvent(new CustomEvent("job-scan:review-group-changed"));
    }
    if (updateHash) window.history.replaceState(null, "", `#${groupId}`);
  };

  const revealFilteredReevaluationCard = (card) => {
    if (!card.hidden || !card.closest('[data-review-block="global"]')) return;
    const sourceControl = document.querySelector("#global-source-filter")?.tomselect;
    if (sourceControl) {
      sourceControl.setValue(Object.keys(sourceControl.options));
    }
    const urlFilter = document.querySelector("#global-url-filter");
    if (urlFilter?.value) {
      urlFilter.value = "";
      urlFilter.dispatchEvent(new Event("input", { bubbles: true }));
    }
    const tagControl = document.querySelector("#global-tag-filter")?.tomselect;
    if (tagControl?.items.length) {
      tagControl.clear();
    }
  };

  const initializeReviewGroupWorkspace = (workspace) => {
    const container = workspace.querySelector(":scope > .review-groups");
    if (!container) return;
    const navigation = workspace.querySelector(":scope > .review-group-nav");
    if (!navigation) return;
    const storageKey = workspace.dataset.reviewOrderKey || reviewGroupOrderKey;
    const panels = reviewGroupPanels(container);
    if (panels.length === 0) return;
    const availableIds = panels.map((panel) => panel.id);
    applyReviewGroupOrder(
      container,
      navigation,
      restoredReviewGroupOrder(storageKey, availableIds),
    );
    selectReviewGroup(
      container,
      navigation,
      requestedReviewGroupId(availableIds, navigation),
    );

    container.addEventListener("wheel", (event) => {
      if (event.ctrlKey || event.deltaY === 0) return;
      const atTop = container.scrollTop <= 0;
      const atBottom = (
        container.scrollTop + container.clientHeight >= container.scrollHeight - 1
      );
      const scrollingUpPastTop = event.deltaY < 0 && atTop;
      const scrollingDownPastBottom = event.deltaY > 0 && atBottom;
      if (!scrollingUpPastTop && !scrollingDownPastBottom) return;

      const page = document.scrollingElement || document.documentElement;
      const pageScrollTop = window.scrollY;
      const maxPageScrollTop = Math.max(0, page.scrollHeight - window.innerHeight);
      const pageCanScroll = scrollingUpPastTop
        ? pageScrollTop > 0
        : pageScrollTop < maxPageScrollTop;
      if (!pageCanScroll) return;

      const pageDelta = event.deltaMode === 1
        ? event.deltaY * 16
        : event.deltaMode === 2
          ? event.deltaY * window.innerHeight
          : event.deltaY;
      event.preventDefault();
      window.scrollBy({ top: pageDelta, behavior: "auto" });
    }, { passive: false });

    let draggedTab = null;
    let draggedJobCard = null;
    const jobDragPreviewScale = 0.6;
    let touchPointerId = null;
    let touchDropTarget = null;
    let touchDropBefore = false;
    let lastTouchJumpTab = null;
    let lastTouchJumpAt = 0;
    const reevaluationJumpKeys = new Map();
    const clearDragState = () => {
      reviewGroupTabs(navigation).forEach((tab) => {
        tab.classList.remove(
          "is-dragging",
          "is-drag-over-before",
          "is-drag-over-after",
        );
      });
    };
    const clearJobDropTargets = () => {
      reviewGroupTabs(navigation).forEach((tab) => {
        tab.classList.remove("is-job-drop-target");
      });
    };
    const clearJobDragState = () => {
      draggedJobCard?.classList.remove("is-job-dragging");
      jobBatchController?.clearDragging();
      clearJobDropTargets();
    };
    const finishJobDrag = () => {
      clearJobDragState();
      draggedJobCard = null;
    };
    const finishDrag = () => {
      draggedTab = null;
      touchPointerId = null;
      touchDropTarget = null;
      touchDropBefore = false;
      clearDragState();
    };
    const dropTarget = (event) => {
      const tab = event.target.closest("[data-review-group-tab]");
      return tab?.parentElement === navigation ? tab : null;
    };
    const commitReviewGroupOrder = (movedTab) => {
      const order = reviewGroupTabs(navigation).map(
        (tab) => tab.dataset.reviewGroupTab,
      );
      applyReviewGroupOrder(container, navigation, order);
      saveReviewGroupOrder(storageKey, order);
      const position = order.indexOf(movedTab.dataset.reviewGroupTab) + 1;
      const label = movedTab.querySelector(".review-group-label")?.textContent.trim();
      const announcement = navigation.querySelector(".review-group-announcement");
      if (announcement && label) {
        announcement.textContent = `Moved ${label} to position ${position} of ${order.length}.`;
      }
    };
    const moveReviewGroupTab = (tab, offset) => {
      const tabs = reviewGroupTabs(navigation);
      const currentIndex = tabs.indexOf(tab);
      const target = tabs[currentIndex + offset];
      if (currentIndex < 0 || !target) return;
      navigation.insertBefore(
        tab,
        offset < 0 ? target : target.nextElementSibling,
      );
      commitReviewGroupOrder(tab);
      tab.focus();
    };
    const focusLatestReevaluationResult = (tab) => {
      const groupId = tab.dataset.reviewGroupTab;
      const group = reviewGroupPanels(container).find(
        (panel) => panel.id === groupId,
      );
      if (!group) return false;
      const candidates = [...group.querySelectorAll(
        ':scope > .card-grid > .job-card[data-reevaluation-status]',
      )].sort((left, right) => (
        Date.parse(right.dataset.reevaluationFinishedAt || "")
        - Date.parse(left.dataset.reevaluationFinishedAt || "")
      ));
      if (candidates.length === 0) return false;
      const lastKey = reevaluationJumpKeys.get(groupId);
      const lastIndex = candidates.findIndex(
        (card) => card.dataset.jobKey === lastKey,
      );
      const nextIndex = lastIndex === -1 ? 0 : (lastIndex + 1) % candidates.length;
      const target = candidates[nextIndex];
      reevaluationJumpKeys.set(groupId, target.dataset.jobKey);
      selectReviewGroup(container, navigation, groupId, true);
      revealFilteredReevaluationCard(target);
      target.focus({ preventScroll: true });
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      return true;
    };

    navigation.addEventListener("click", (event) => {
      const tab = dropTarget(event);
      if (!tab) return;
      event.preventDefault();
      if (workspace.closest('[data-review-block="global"]')) {
        jobBatchController?.exit();
      }
      selectReviewGroup(
        container,
        navigation,
        tab.dataset.reviewGroupTab,
        true,
      );
    });
    navigation.addEventListener("dblclick", (event) => {
      const tab = dropTarget(event);
      if (!tab) return;
      if (focusLatestReevaluationResult(tab)) event.preventDefault();
    });
    navigation.addEventListener("keydown", (event) => {
      const tab = dropTarget(event);
      if (!tab) return;
      if (
        event.key === "Enter"
        && tab.getAttribute("aria-current") === "page"
        && focusLatestReevaluationResult(tab)
      ) {
        event.preventDefault();
        return;
      }
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      moveReviewGroupTab(tab, event.key === "ArrowUp" ? -1 : 1);
    });
    navigation.addEventListener("dragstart", (event) => {
      draggedTab = dropTarget(event);
      if (!draggedTab) return;
      draggedTab.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedTab.dataset.reviewGroupTab);
    });
    navigation.addEventListener("dragover", (event) => {
      const target = dropTarget(event);
      if (draggedJobCard) {
        const sourceGroup = draggedJobCard.closest(".job-group");
        clearJobDropTargets();
        if (!target || sourceGroup?.id === target.dataset.reviewGroupTab) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        target.classList.add("is-job-drop-target");
        return;
      }
      if (!draggedTab || !target || target === draggedTab) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      clearDragState();
      draggedTab.classList.add("is-dragging");
      const bounds = target.getBoundingClientRect();
      const before = event.clientY <= bounds.top + bounds.height / 2;
      target.classList.add(
        before ? "is-drag-over-before" : "is-drag-over-after",
      );
    });
    navigation.addEventListener("dragleave", (event) => {
      if (
        draggedJobCard
        && (!event.relatedTarget || !navigation.contains(event.relatedTarget))
      ) {
        clearJobDropTargets();
      }
    });
    navigation.addEventListener("drop", (event) => {
      const target = dropTarget(event);
      if (draggedJobCard) {
        const card = draggedJobCard;
        const sourceGroup = card.closest(".job-group");
        if (!target || sourceGroup?.id === target.dataset.reviewGroupTab) return;
        event.preventDefault();
        const form = card.querySelector('[data-job-action="status"]');
        const select = form?.elements.namedItem("status");
        const targetStatus = target.dataset.reviewGroupTab;
        finishJobDrag();
        if (jobBatchController?.isActiveFor(card)) {
          if (targetStatus) void jobBatchController.moveTo(targetStatus);
          return;
        }
        if (!form || !select || !targetStatus) return;
        select.value = targetStatus;
        if (select.value === targetStatus) form.requestSubmit();
        return;
      }
      if (!draggedTab || !target || target === draggedTab) return;
      event.preventDefault();
      const bounds = target.getBoundingClientRect();
      const before = event.clientY <= bounds.top + bounds.height / 2;
      navigation.insertBefore(
        draggedTab,
        before ? target : target.nextElementSibling,
      );
      commitReviewGroupOrder(draggedTab);
      finishDrag();
    });
    navigation.addEventListener("dragend", finishDrag);
    container.addEventListener("dragstart", (event) => {
      const card = event.target.closest("[data-job-drag-source]");
      if (!card) return;
      jobBatchController?.cancelLongPress();
      if (
        jobBatchController?.isActiveFor(card)
        && !jobBatchController.isSelected(card)
      ) {
        event.preventDefault();
        return;
      }
      const bounds = card.getBoundingClientRect();
      const preview = document.createElement("div");
      const previewCard = card.cloneNode(true);
      preview.className = "card-grid";
      preview.setAttribute("aria-hidden", "true");
      Object.assign(preview.style, {
        position: "fixed",
        top: "0",
        left: "0",
        width: `${bounds.width * jobDragPreviewScale}px`,
        height: `${bounds.height * jobDragPreviewScale}px`,
        overflow: "hidden",
        pointerEvents: "none",
      });
      Object.assign(previewCard.style, {
        width: `${bounds.width}px`,
        height: `${bounds.height}px`,
        transform: `scale(${jobDragPreviewScale})`,
        transformOrigin: "top left",
      });
      preview.append(previewCard);
      document.body.append(preview);
      event.dataTransfer.setDragImage(
        preview,
        (event.clientX - bounds.left) * jobDragPreviewScale,
        (event.clientY - bounds.top) * jobDragPreviewScale,
      );
      requestAnimationFrame(() => preview.remove());
      draggedJobCard = card;
      if (jobBatchController?.isActiveFor(card)) {
        jobBatchController.markDragging();
      } else {
        card.classList.add("is-job-dragging");
      }
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", card.dataset.jobKey);
    });
    container.addEventListener("dragend", finishJobDrag);
    navigation.addEventListener("pointerdown", (event) => {
      if (
        event.pointerType === "mouse"
        || !event.target.closest("[data-review-group-drag-handle]")
      ) {
        return;
      }
      draggedTab = dropTarget(event);
      if (!draggedTab) return;
      event.preventDefault();
      touchPointerId = event.pointerId;
      draggedTab.classList.add("is-dragging");
    });
    navigation.addEventListener("pointermove", (event) => {
      if (event.pointerId !== touchPointerId || !draggedTab) return;
      const target = dropTarget(event);
      if (!target || target === draggedTab) return;
      event.preventDefault();
      clearDragState();
      draggedTab.classList.add("is-dragging");
      const bounds = target.getBoundingClientRect();
      touchDropTarget = target;
      touchDropBefore = event.clientY <= bounds.top + bounds.height / 2;
      target.classList.add(
        touchDropBefore ? "is-drag-over-before" : "is-drag-over-after",
      );
    });
    navigation.addEventListener("pointerup", (event) => {
      if (event.pointerType === "mouse" || draggedTab) return;
      const tab = dropTarget(event);
      if (!tab || event.target.closest("[data-review-group-drag-handle]")) return;
      const isDoubleTap = (
        lastTouchJumpTab === tab
        && event.timeStamp - lastTouchJumpAt <= 500
      );
      lastTouchJumpTab = isDoubleTap ? null : tab;
      lastTouchJumpAt = isDoubleTap ? 0 : event.timeStamp;
      if (isDoubleTap && focusLatestReevaluationResult(tab)) {
        event.preventDefault();
      }
    });
    window.addEventListener("pointerup", (event) => {
      if (event.pointerId !== touchPointerId || !draggedTab) return;
      if (touchDropTarget) {
        navigation.insertBefore(
          draggedTab,
          touchDropBefore ? touchDropTarget : touchDropTarget.nextElementSibling,
        );
        commitReviewGroupOrder(draggedTab);
      }
      finishDrag();
    });
    window.addEventListener("pointercancel", (event) => {
      if (event.pointerId === touchPointerId) finishDrag();
    });
    window.addEventListener("hashchange", () => {
      const groupId = requestedReviewGroupId(availableIds, navigation);
      if (groupId) selectReviewGroup(container, navigation, groupId);
    });
  };

  const reviewCardForJob = (root, jobKey) => [
    ...root.querySelectorAll("article.job-card[data-job-key]"),
  ].find((card) => card.dataset.jobKey === jobKey);

  const reviewGroupById = (block, groupId) => [
    ...block.querySelectorAll(".review-groups > .job-group"),
  ].find((group) => group.id === groupId);

  const syncReviewGroupEmptyState = (group) => {
    const grid = group.querySelector(":scope > .card-grid");
    if (!grid) return;
    const cards = grid.querySelectorAll(":scope > .job-card");
    const empty = grid.querySelector(":scope > .empty");
    if (cards.length > 0) {
      empty?.remove();
      return;
    }
    if (!empty) {
      const message = document.createElement("p");
      message.className = "empty";
      message.textContent = "No jobs in this group.";
      grid.append(message);
    }
  };

  const insertRefreshedCard = (liveBlock, refreshedCard, replacement) => {
    const refreshedGroup = refreshedCard.closest(".job-group");
    const liveGroup = refreshedGroup
      ? reviewGroupById(liveBlock, refreshedGroup.id)
      : null;
    if (!liveGroup) return;
    const liveGrid = liveGroup.querySelector(":scope > .card-grid");
    if (!liveGrid) return;
    const refreshedCards = [
      ...refreshedGroup.querySelectorAll(":scope > .card-grid > .job-card"),
    ];
    const refreshedIndex = refreshedCards.indexOf(refreshedCard);
    const nextLiveCard = refreshedCards
      .slice(refreshedIndex + 1)
      .map((card) => reviewCardForJob(liveGroup, card.dataset.jobKey))
      .find(Boolean);
    liveGrid.insertBefore(replacement, nextLiveCard || null);
    syncReviewGroupEmptyState(liveGroup);
  };

  const refreshOpenJobCard = (liveCard, refreshedCard) => {
    const liveDialog = liveCard.querySelector(
      ":scope > [data-job-detail-dialog]",
    );
    const refreshedDialog = refreshedCard.querySelector(
      ":scope > [data-job-detail-dialog]",
    );
    if (!liveDialog?.open || !refreshedDialog) return false;

    [...liveCard.attributes].forEach((attribute) => {
      if (!refreshedCard.hasAttribute(attribute.name)) {
        liveCard.removeAttribute(attribute.name);
      }
    });
    [...refreshedCard.attributes].forEach((attribute) => {
      liveCard.setAttribute(attribute.name, attribute.value);
    });
    [...liveCard.children].forEach((child) => {
      if (child !== liveDialog) child.remove();
    });
    [...refreshedCard.children].forEach((child) => {
      if (child !== refreshedDialog) {
        liveCard.insertBefore(document.importNode(child, true), liveDialog);
      }
    });
    liveDialog.replaceChildren(
      ...[...refreshedDialog.childNodes].map(
        (node) => document.importNode(node, true),
      ),
    );
    return true;
  };

  const reconcileReviewJob = (
    refreshedDocument,
    jobKey,
    { preserveOpenDetail = false } = {},
  ) => {
    document.querySelectorAll("[data-review-block]").forEach((liveBlock) => {
      const blockName = liveBlock.dataset.reviewBlock;
      const refreshedBlock = [
        ...refreshedDocument.querySelectorAll("[data-review-block]"),
      ].find((block) => block.dataset.reviewBlock === blockName);
      if (!refreshedBlock) return;
      const liveCard = reviewCardForJob(liveBlock, jobKey);
      const refreshedCard = reviewCardForJob(refreshedBlock, jobKey);
      const previousGroup = liveCard?.closest(".job-group");
      if (!refreshedCard) {
        liveCard?.remove();
        if (previousGroup) syncReviewGroupEmptyState(previousGroup);
        return;
      }
      if (
        preserveOpenDetail
        && liveCard
        && refreshOpenJobCard(liveCard, refreshedCard)
      ) {
        return;
      }
      const replacement = document.importNode(refreshedCard, true);
      liveCard?.remove();
      insertRefreshedCard(liveBlock, refreshedCard, replacement);
      if (previousGroup) syncReviewGroupEmptyState(previousGroup);
    });
    updateReviewGroupCounts();
    document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
  };

  const fetchReviewDocument = async (url) => {
    const response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "text/html" },
    });
    if (!response.ok) {
      throw new Error(`Could not refresh Review (${response.status}).`);
    }
    return new DOMParser().parseFromString(
      await response.text(),
      "text/html",
    );
  };

  const refreshReviewJob = async (jobKey, options = {}) => {
    const refreshedDocument = await fetchReviewDocument(window.location.href);
    reconcileReviewJob(refreshedDocument, jobKey, options);
  };

  const syncAppliedPreview = (liveCard, refreshedCard) => {
    const liveApplied = liveCard.querySelector("[data-job-preview-applied]");
    const refreshedApplied = refreshedCard.querySelector("[data-job-preview-applied]");
    if (liveApplied && refreshedApplied) {
      liveApplied.replaceWith(document.importNode(refreshedApplied, true));
    } else if (liveApplied) {
      liveApplied.remove();
    } else if (refreshedApplied) {
      liveCard.querySelector(".job-preview-location")?.append(
        document.importNode(refreshedApplied, true),
      );
    }
    liveCard.dataset.appliedAt = refreshedCard.dataset.appliedAt || "";
  };

  const refreshJobLifecycle = async (card, jobKey) => {
    const refreshedDocument = await fetchReviewDocument(window.location.href);
    const blockName = card.closest("[data-review-block]")?.dataset.reviewBlock;
    const refreshedBlock = [
      ...refreshedDocument.querySelectorAll("[data-review-block]"),
    ].find((block) => block.dataset.reviewBlock === blockName);
    const liveLifecycle = card.querySelector("[data-job-lifecycle]");
    const refreshedCard = reviewCardForJob(
      refreshedBlock || refreshedDocument,
      jobKey,
    );
    const refreshedLifecycle = refreshedCard?.querySelector("[data-job-lifecycle]");
    if (!liveLifecycle || !refreshedCard || !refreshedLifecycle) {
      throw new Error("Could not refresh this job lifecycle.");
    }
    liveLifecycle.replaceWith(document.importNode(refreshedLifecycle, true));
    syncAppliedPreview(card, refreshedCard);
    document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
  };

  const reconcileGlobalJobs = (refreshedDocument) => {
    const liveBlock = document.querySelector('[data-review-block="global"]');
    const refreshedBlock = refreshedDocument.querySelector(
      '[data-review-block="global"]',
    );
    const liveGroups = liveBlock?.querySelector(
      "[data-review-workspace] > .review-groups",
    );
    const refreshedGroups = refreshedBlock?.querySelector(
      "[data-review-workspace] > .review-groups",
    );
    if (
      !liveBlock
      || !refreshedBlock
      || !liveGroups
      || !refreshedGroups
    ) {
      throw new Error("Could not refresh Job Tracker.");
    }

    const refreshedPanels = new Map(
      reviewGroupPanels(refreshedGroups).map((panel) => [panel.id, panel]),
    );
    const scrollTop = liveGroups.scrollTop;
    reviewGroupPanels(liveGroups).forEach((livePanel) => {
      const refreshedPanel = refreshedPanels.get(livePanel.id);
      if (!refreshedPanel) {
        livePanel.remove();
        return;
      }
      const replacement = document.importNode(refreshedPanel, true);
      replacement.hidden = livePanel.hidden;
      livePanel.replaceWith(replacement);
      refreshedPanels.delete(livePanel.id);
    });
    refreshedPanels.forEach((panel) => {
      const replacement = document.importNode(panel, true);
      replacement.hidden = true;
      liveGroups.append(replacement);
    });
    liveGroups.scrollTop = scrollTop;
    updateReviewGroupCounts();
    document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
  };

  const responseError = async (response, fallback) => {
    try {
      const payload = await response.json();
      return typeof payload.detail === "string" ? payload.detail : fallback;
    } catch (_error) {
      return fallback;
    }
  };

  const initializeJobBatchMode = () => {
    const reviewBlock = document.querySelector('[data-review-block="global"]');
    const toolbar = document.querySelector("[data-job-batch-toolbar]");
    const selectedCount = toolbar?.querySelector("[data-batch-selected-count]");
    const moveDialog = reviewBlock?.querySelector("[data-job-batch-move-dialog]");
    const moveMessage = moveDialog?.querySelector("[data-batch-move-message]");
    const moveTarget = moveDialog?.querySelector("[data-batch-target-group]");
    const moveError = moveDialog?.querySelector("[data-job-batch-move-error]");
    const confirmMove = moveDialog?.querySelector("[data-confirm-batch-move]");
    const deleteDialog = reviewBlock?.querySelector(
      "[data-job-batch-delete-dialog]",
    );
    const deleteMessage = deleteDialog?.querySelector("[data-batch-delete-message]");
    const deleteConfirmation = deleteDialog?.querySelector(
      "[data-batch-delete-confirmation]",
    );
    const deleteError = deleteDialog?.querySelector("[data-job-batch-delete-error]");
    const confirmDelete = deleteDialog?.querySelector("[data-confirm-batch-delete]");
    if (
      !reviewBlock
      || !toolbar
      || !selectedCount
      || !moveDialog
      || !moveMessage
      || !moveTarget
      || !confirmMove
      || !deleteDialog
      || !deleteMessage
      || !deleteConfirmation
      || !confirmDelete
    ) {
      return null;
    }

    const selectedKeys = new Set();
    const longPressDelay = 1200;
    const movementTolerance = 10;
    let sourceGroupId = null;
    let longPress = null;
    let suppressNextClickCard = null;
    let busy = false;

    const cards = () => [
      ...reviewBlock.querySelectorAll(
        'article.job-card[data-status-scope="global"][data-job-key]',
      ),
    ];
    const selectedCards = () => cards().filter(
      (card) => selectedKeys.has(card.dataset.jobKey),
    );
    const isActive = () => selectedKeys.size > 0;
    const showError = (element, message) => {
      if (!element) return;
      element.textContent = message;
      element.hidden = false;
    };
    const clearError = (element) => {
      if (!element) return;
      element.textContent = "";
      element.hidden = true;
    };
    const clearLongPress = () => {
      if (longPress?.timer) window.clearTimeout(longPress.timer);
      longPress = null;
    };
    const render = () => {
      const active = isActive();
      reviewBlock.classList.toggle("is-batch-mode", active);
      toolbar.hidden = !active;
      selectedCount.textContent = `${selectedKeys.size} selected`;
      cards().forEach((card) => {
        const selected = selectedKeys.has(card.dataset.jobKey);
        card.classList.toggle("is-batch-selected", selected);
        if (active) {
          const originalLabel = card.dataset.jobBatchOriginalLabel
            ?? card.getAttribute("aria-label")
            ?? "";
          const selectionAction = selected
            ? "remove it from the selection"
            : "add it to the selection";
          card.dataset.jobBatchOriginalLabel = originalLabel;
          card.setAttribute(
            "aria-label",
            `${selected ? "Selected" : "Not selected"}. ${originalLabel}. Press Enter or Space to ${selectionAction}.`,
          );
          card.removeAttribute("aria-haspopup");
        } else {
          const originalLabel = card.dataset.jobBatchOriginalLabel;
          if (originalLabel !== undefined) {
            card.setAttribute("aria-label", originalLabel);
            delete card.dataset.jobBatchOriginalLabel;
          }
          card.setAttribute("aria-haspopup", "dialog");
        }
      });
      document.dispatchEvent(new CustomEvent("job-scan:batch-selection-changed"));
    };
    const closeDialog = (dialog) => {
      if (dialog.open) dialog.close("cancel");
    };
    const exit = () => {
      clearLongPress();
      selectedKeys.clear();
      sourceGroupId = null;
      setBusy(false);
      closeDialog(moveDialog);
      closeDialog(deleteDialog);
      render();
    };
    const toggleCard = (card) => {
      const groupId = card.closest(".job-group")?.id;
      const jobKey = card.dataset.jobKey;
      if (!groupId || !jobKey) return;
      if (!isActive()) sourceGroupId = groupId;
      if (groupId !== sourceGroupId) return;
      if (selectedKeys.has(jobKey)) {
        selectedKeys.delete(jobKey);
      } else {
        selectedKeys.add(jobKey);
      }
      if (selectedKeys.size === 0) sourceGroupId = null;
      render();
    };
    const setBusy = (value) => {
      busy = value;
      toolbar.querySelectorAll("button").forEach((button) => {
        button.disabled = value;
      });
      confirmMove.disabled = value || moveTarget.value === sourceGroupId;
      confirmDelete.disabled = value || deleteConfirmation.value !== "Delete all";
    };
    const selectGroupWithoutChangingPage = (groupId) => {
      const workspace = reviewBlock.querySelector("[data-review-workspace]");
      const container = workspace?.querySelector(":scope > .review-groups");
      const navigation = workspace?.querySelector(":scope > .review-group-nav");
      if (container && navigation) {
        selectReviewGroup(container, navigation, groupId);
      }
    };
    const refreshAfterMutation = async (targetGroupId = null) => {
      const refreshedDocument = await fetchReviewDocument(window.location.href);
      reconcileGlobalJobs(refreshedDocument);
      if (targetGroupId) selectGroupWithoutChangingPage(targetGroupId);
      exit();
    };
    const moveTo = async (targetStatus, errorElement = null) => {
      if (
        busy
        || !isActive()
        || !targetStatus
        || targetStatus === sourceGroupId
      ) {
        return false;
      }
      setBusy(true);
      clearError(errorElement);
      try {
        const response = await fetch("/api/job-tracker/jobs/batch-status", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            keys: [...selectedKeys],
            status: targetStatus,
          }),
        });
        if (!response.ok) {
          throw new Error(await responseError(
            response,
            "Could not move the selected jobs.",
          ));
        }
        await refreshAfterMutation(targetStatus);
        return true;
      } catch (error) {
        if (errorElement) {
          showError(errorElement, error.message);
        } else {
          window.alert(error.message);
        }
        setBusy(false);
        return false;
      }
    };

    reviewBlock.addEventListener("pointerdown", (event) => {
      const card = event.target.closest(
        'article.job-card[data-status-scope="global"]',
      );
      if (
        !card
        || event.button !== 0
        || event.target.closest("a, button, input, select, label, form, dialog")
      ) {
        return;
      }
      clearLongPress();
      longPress = {
        card,
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        activated: false,
        timer: window.setTimeout(() => {
          if (!longPress || longPress.card !== card) return;
          longPress.activated = true;
          toggleCard(card);
        }, longPressDelay),
      };
    });
    reviewBlock.addEventListener("pointermove", (event) => {
      if (!longPress || event.pointerId !== longPress.pointerId) return;
      if (
        Math.hypot(
          event.clientX - longPress.startX,
          event.clientY - longPress.startY,
        ) > movementTolerance
      ) {
        clearLongPress();
      }
    });
    const finishPointer = (event) => {
      if (!longPress || event.pointerId !== longPress.pointerId) return;
      const activatedCard = longPress.activated ? longPress.card : null;
      clearLongPress();
      if (!activatedCard) return;
      suppressNextClickCard = activatedCard;
      window.setTimeout(() => {
        if (suppressNextClickCard === activatedCard) suppressNextClickCard = null;
      }, 0);
    };
    reviewBlock.addEventListener("pointerup", finishPointer);
    reviewBlock.addEventListener("pointercancel", finishPointer);
    reviewBlock.addEventListener("click", (event) => {
      const card = event.target.closest(
        'article.job-card[data-status-scope="global"]',
      );
      if (!card || !isActive()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (card === suppressNextClickCard) {
        suppressNextClickCard = null;
        return;
      }
      toggleCard(card);
    }, true);
    reviewBlock.addEventListener("keydown", (event) => {
      const card = event.target.closest(
        'article.job-card[data-status-scope="global"]',
      );
      if (
        !card
        || !isActive()
        || event.target !== card
        || !["Enter", " "].includes(event.key)
      ) {
        return;
      }
      event.preventDefault();
      event.stopImmediatePropagation();
      toggleCard(card);
    }, true);

    toolbar.querySelector("[data-exit-batch]").addEventListener("click", exit);
    toolbar.querySelector("[data-batch-move]").addEventListener("click", () => {
      clearError(moveError);
      [...moveTarget.options].forEach((option) => {
        option.disabled = option.value === sourceGroupId;
      });
      const firstTarget = [...moveTarget.options].find((option) => !option.disabled);
      if (firstTarget) moveTarget.value = firstTarget.value;
      moveMessage.textContent = `Move ${selectedKeys.size} selected ${
        selectedKeys.size === 1 ? "job" : "jobs"
      } to:`;
      confirmMove.disabled = !firstTarget;
      moveDialog.showModal();
      moveTarget.focus();
    });
    moveTarget.addEventListener("change", () => {
      confirmMove.disabled = busy || moveTarget.value === sourceGroupId;
    });
    moveDialog.querySelector("[data-cancel-batch-move]").addEventListener(
      "click",
      () => moveDialog.close("cancel"),
    );
    confirmMove.addEventListener("click", async () => {
      if (confirmMove.disabled) return;
      const moved = await moveTo(moveTarget.value, moveError);
      if (moved && moveDialog.open) moveDialog.close("moved");
    });

    toolbar.querySelector("[data-batch-delete]").addEventListener("click", () => {
      clearError(deleteError);
      deleteConfirmation.value = "";
      confirmDelete.disabled = true;
      deleteMessage.textContent = `This permanently deletes ${selectedKeys.size} selected ${
        selectedKeys.size === 1 ? "job" : "jobs"
      } and their Job Tracker history.`;
      deleteDialog.showModal();
      deleteConfirmation.focus();
    });
    deleteConfirmation.addEventListener("input", () => {
      confirmDelete.disabled = busy || deleteConfirmation.value !== "Delete all";
    });
    deleteDialog.querySelector("[data-cancel-batch-delete]").addEventListener(
      "click",
      () => deleteDialog.close("cancel"),
    );
    confirmDelete.addEventListener("click", async () => {
      if (busy || confirmDelete.disabled || !isActive()) return;
      setBusy(true);
      clearError(deleteError);
      try {
        const response = await fetch("/api/job-tracker/jobs/batch", {
          method: "DELETE",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            keys: [...selectedKeys],
            confirmation_text: deleteConfirmation.value,
          }),
        });
        if (!response.ok) {
          throw new Error(await responseError(
            response,
            "Could not delete the selected jobs.",
          ));
        }
        await refreshAfterMutation();
      } catch (error) {
        showError(deleteError, error.message);
        setBusy(false);
      }
    });
    document.addEventListener("click", (event) => {
      const step = event.target.closest("[data-nav-step]");
      if (step && step.dataset.navStep !== "job-tracker") exit();
    }, true);
    document.addEventListener("keydown", (event) => {
      if (
        event.key === "Escape"
        && isActive()
        && !moveDialog.open
        && !deleteDialog.open
      ) {
        exit();
      }
    });
    document.addEventListener("job-scan:review-updated", () => {
      if (!isActive()) return;
      const availableKeys = new Set(cards().map((card) => card.dataset.jobKey));
      [...selectedKeys].forEach((key) => {
        if (!availableKeys.has(key)) selectedKeys.delete(key);
      });
      if (selectedKeys.size === 0) sourceGroupId = null;
      render();
    });
    document.addEventListener("job-scan:ats-started", exit);

    return {
      exit,
      isActiveFor: (card) => isActive() && reviewBlock.contains(card),
      isSelected: (card) => selectedKeys.has(card.dataset.jobKey),
      markDragging: () => {
        selectedCards().forEach((card) => card.classList.add("is-job-dragging"));
      },
      clearDragging: () => {
        cards().forEach((card) => card.classList.remove("is-job-dragging"));
      },
      cancelLongPress: clearLongPress,
      moveTo,
    };
  };

  const initializeTrackerGroupSettings = () => {
    const dialog = document.querySelector("[data-tracker-group-dialog]");
    const opener = document.querySelector("[data-open-tracker-groups]");
    const deleteDialog = document.querySelector(
      "[data-tracker-group-delete-dialog]",
    );
    if (!dialog || !opener || !deleteDialog) return;
    const reviewBlock = document.querySelector('[data-review-block="global"]');
    const workspace = reviewBlock?.querySelector("[data-review-workspace]");
    const navigation = workspace?.querySelector(":scope > .review-group-nav");
    const groupPanels = workspace?.querySelector(":scope > .review-groups");
    const groupList = dialog.querySelector("[data-tracker-group-list]");
    const errorMessage = dialog.querySelector("[data-tracker-group-error]");
    const form = dialog.querySelector("form");
    const closeButton = dialog.querySelector("[data-close-tracker-groups]");
    const newName = dialog.querySelector("[data-new-tracker-group-name]");
    const deleteMessage = deleteDialog.querySelector(
      "[data-tracker-group-delete-message]",
    );
    const confirmationField = deleteDialog.querySelector(
      "[data-tracker-group-confirmation-field]",
    );
    const confirmationName = deleteDialog.querySelector(
      "[data-tracker-group-confirmation-name]",
    );
    const confirmationInput = deleteDialog.querySelector(
      "[data-tracker-group-confirmation-input]",
    );
    const deleteError = deleteDialog.querySelector(
      "[data-tracker-group-delete-error]",
    );
    const confirmDelete = deleteDialog.querySelector(
      "[data-confirm-tracker-group-delete]",
    );
    let pendingDelete = null;

    const showError = (target, message) => {
      if (!target) return;
      target.textContent = message;
      target.hidden = false;
    };
    const clearError = (target) => {
      if (!target) return;
      target.textContent = "";
      target.hidden = true;
    };
    const requestGroupChange = async (url, method, payload, fallback) => {
      const response = await fetch(url, {
        method,
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        throw new Error(await responseError(response, fallback));
      }
      return response.json();
    };
    const statusSelector = (groupId) => (
      `[data-tracker-status-name="${CSS.escape(groupId)}"]`
    );
    const groupTab = (groupId) => navigation?.querySelector(
      `[data-review-group-tab="${CSS.escape(groupId)}"]`,
    );
    const addStatusOption = (group) => {
      document.querySelectorAll('select[name="status"]').forEach((select) => {
        const option = document.createElement("option");
        option.value = group.id;
        option.textContent = group.name;
        select.append(option);
      });
      const batchTarget = document.querySelector("[data-batch-target-group]");
      if (batchTarget) {
        const option = document.createElement("option");
        option.value = group.id;
        option.textContent = group.name;
        batchTarget.append(option);
      }
    };
    const addGroupRow = (group) => {
      const template = groupList?.querySelector("[data-tracker-group-row]");
      if (!template) return;
      const row = template.cloneNode(true);
      row.dataset.groupId = group.id;
      row.dataset.groupName = group.name;
      row.dataset.groupCount = "0";
      const input = row.querySelector("[data-tracker-group-name]");
      input.value = group.name;
      input.setAttribute("aria-label", `Rename ${group.name}`);
      const hiddenLabel = row.querySelector("label .visually-hidden");
      if (hiddenLabel) hiddenLabel.textContent = `${group.name} group name`;
      row.querySelector(".tracker-group-job-count").textContent = "0 jobs";
      const deleteButton = row.querySelector("[data-delete-tracker-group]");
      deleteButton.disabled = false;
      deleteButton.removeAttribute("aria-disabled");
      deleteButton.removeAttribute("title");
      row.querySelector("[data-save-tracker-group]").disabled = false;
      groupList.append(row);
    };
    const addGroupNavigation = (group) => {
      const template = navigation?.querySelector("[data-review-group-tab]");
      if (!template || !groupPanels) return;
      const tab = template.cloneNode(true);
      tab.href = `#${group.id}`;
      tab.dataset.reviewGroupTab = group.id;
      tab.removeAttribute("aria-current");
      tab.classList.remove(
        "is-dragging",
        "is-drag-over-before",
        "is-drag-over-after",
        "is-job-drop-target",
      );
      tab.querySelector(".review-group-label").textContent = group.name;
      const count = tab.querySelector("[data-review-group-count]");
      count.dataset.reviewGroupCount = group.id;
      count.textContent = "0";
      const notice = tab.querySelector("[data-review-group-notice-count]");
      notice.dataset.reviewGroupNoticeCount = group.id;
      notice.textContent = "";
      notice.hidden = true;
      navigation.insertBefore(
        tab,
        navigation.querySelector(".review-group-announcement"),
      );

      const panel = document.createElement("section");
      panel.className = "job-group";
      panel.id = group.id;
      panel.hidden = true;
      panel.setAttribute("aria-labelledby", `${group.id}-title`);
      const title = document.createElement("h2");
      title.className = "visually-hidden";
      title.id = `${group.id}-title`;
      title.textContent = group.name;
      const grid = document.createElement("div");
      grid.className = "card-grid";
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No jobs in this group.";
      grid.append(empty);
      panel.append(title, grid);
      groupPanels.append(panel);
    };
    const renameGroupInPage = (group) => {
      const row = groupList?.querySelector(
        `[data-group-id="${CSS.escape(group.id)}"]`,
      );
      if (row) {
        row.dataset.groupName = group.name;
        const input = row.querySelector("[data-tracker-group-name]");
        input.value = group.name;
        input.setAttribute("aria-label", `Rename ${group.name}`);
        const hiddenLabel = row.querySelector("label .visually-hidden");
        if (hiddenLabel) hiddenLabel.textContent = `${group.name} group name`;
        if (group.id === "saved") {
          row.querySelector("[data-delete-tracker-group]").title =
            `${group.name} is the required starting group`;
        }
      }
      const tab = groupTab(group.id);
      if (tab) tab.querySelector(".review-group-label").textContent = group.name;
      const panelTitle = groupPanels?.querySelector(
        `#${CSS.escape(group.id)} > h2`,
      );
      if (panelTitle) panelTitle.textContent = group.name;
      document.querySelectorAll(
        `select[name="status"] option[value="${CSS.escape(group.id)}"]`,
      ).forEach((option) => { option.textContent = group.name; });
      document.querySelectorAll(
        `[data-batch-target-group] option[value="${CSS.escape(group.id)}"]`,
      ).forEach((option) => { option.textContent = group.name; });
      document.querySelectorAll(statusSelector(group.id)).forEach((label) => {
        label.textContent = group.name;
      });
      document.querySelectorAll(
        `[data-lifecycle-status="${CSS.escape(group.id)}"]`,
      ).forEach((event) => {
        event.dataset.lifecycleStatusLabel = group.name;
        event.querySelectorAll("[data-lifecycle-date-input]").forEach((input) => {
          input.setAttribute("aria-label", `Change ${group.name} date`);
        });
      });
      if (group.id === "saved") {
        const importButton = document.querySelector(
          "#manual-job-dialog [data-submit-manual-job]",
        );
        if (importButton) {
          importButton.dataset.savedGroupName = group.name;
          importButton.dataset.defaultLabel = `Import to ${group.name}`;
          if (!importButton.disabled) importButton.textContent = `Import to ${group.name}`;
        }
      }
    };
    const reindexLifecycle = (card) => {
      [
        ...card.querySelectorAll("[data-lifecycle-step]"),
      ].forEach((event, index) => {
        event.dataset.lifecycleEventIndex = String(index);
        event.querySelectorAll("[data-lifecycle-date-input]").forEach((input) => {
          input.dataset.lifecycleEventIndex = String(index);
        });
        event.querySelectorAll("[data-lifecycle-time]").forEach((time) => {
          time.dataset.lifecycleTime = String(index);
        });
      });
      [
        ...card.querySelectorAll("[data-lifecycle-event]"),
      ].forEach((event, index) => {
        event.querySelectorAll("[data-lifecycle-date-input]").forEach((input) => {
          input.dataset.lifecycleEventIndex = String(index);
        });
        event.querySelectorAll("[data-lifecycle-time]").forEach((time) => {
          time.dataset.lifecycleTime = String(index);
        });
      });
    };
    const removeGroupFromPage = (groupId) => {
      const tab = groupTab(groupId);
      const wasSelected = tab?.getAttribute("aria-current") === "page";
      groupList?.querySelector(
        `[data-group-id="${CSS.escape(groupId)}"]`,
      )?.remove();
      tab?.remove();
      groupPanels?.querySelector(`#${CSS.escape(groupId)}`)?.remove();
      document.querySelectorAll(
        `select[name="status"] option[value="${CSS.escape(groupId)}"]`,
      ).forEach((option) => { option.remove(); });
      document.querySelectorAll(
        `[data-batch-target-group] option[value="${CSS.escape(groupId)}"]`,
      ).forEach((option) => { option.remove(); });
      reviewBlock?.querySelectorAll("article.job-card").forEach((card) => {
        card.querySelectorAll(
          `[data-lifecycle-status="${CSS.escape(groupId)}"]`,
        ).forEach((event) => { event.remove(); });
        if (groupId === "applied") {
          card.querySelector("[data-job-preview-applied]")?.remove();
          card.dataset.appliedAt = "";
        }
        reindexLifecycle(card);
      });
      if (wasSelected && groupPanels && navigation) {
        const fallback = reviewGroupTabs(navigation)[0]?.dataset.reviewGroupTab;
        if (fallback) selectReviewGroup(groupPanels, navigation, fallback, true);
      }
      if (workspace && navigation) {
        saveReviewGroupOrder(
          workspace.dataset.reviewOrderKey || reviewGroupOrderKey,
          reviewGroupTabs(navigation).map((item) => item.dataset.reviewGroupTab),
        );
      }
      document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
    };

    opener.addEventListener("click", () => {
      clearError(errorMessage);
      if (!dialog.open) dialog.showModal();
    });

    closeButton?.addEventListener("click", () => dialog.close("cancel"));
    form?.addEventListener("submit", (event) => {
      event.preventDefault();
      const activeInput = document.activeElement;
      if (activeInput === newName) {
        dialog.querySelector("[data-create-tracker-group]")?.click();
        return;
      }
      activeInput
        ?.closest("[data-tracker-group-row]")
        ?.querySelector("[data-save-tracker-group]")
        ?.click();
    });
    dialog.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      if (event.target === newName) {
        event.preventDefault();
        dialog.querySelector("[data-create-tracker-group]")?.click();
        return;
      }
      const row = event.target.closest("[data-tracker-group-row]");
      if (!row || !event.target.matches("[data-tracker-group-name]")) return;
      event.preventDefault();
      row.querySelector("[data-save-tracker-group]")?.click();
    });

    dialog.addEventListener("click", async (event) => {
      if (event.target === dialog) {
        dialog.close("cancel");
        return;
      }
      const createButton = event.target.closest("[data-create-tracker-group]");
      if (createButton) {
        const name = newName?.value.trim() || "";
        if (!name) {
          showError(errorMessage, "Enter a group name.");
          newName?.focus();
          return;
        }
        createButton.disabled = true;
        clearError(errorMessage);
        try {
          const group = await requestGroupChange(
            "/api/tracker-groups",
            "POST",
            { name },
            "Could not add this group.",
          );
          addGroupRow(group);
          addGroupNavigation(group);
          addStatusOption(group);
          newName.value = "";
          createButton.disabled = false;
          document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
        } catch (error) {
          showError(errorMessage, error.message);
          createButton.disabled = false;
        }
        return;
      }

      const row = event.target.closest("[data-tracker-group-row]");
      if (!row) return;
      const groupId = row.dataset.groupId;
      const groupName = row.dataset.groupName;
      const saveButton = event.target.closest("[data-save-tracker-group]");
      if (saveButton) {
        const input = row.querySelector("[data-tracker-group-name]");
        const name = input?.value.trim() || "";
        if (!name) {
          showError(errorMessage, "Enter a group name.");
          input?.focus();
          return;
        }
        saveButton.disabled = true;
        clearError(errorMessage);
        try {
          const group = await requestGroupChange(
            `/api/tracker-groups/${encodeURIComponent(groupId)}`,
            "PUT",
            { name },
            "Could not rename this group.",
          );
          renameGroupInPage(group);
          saveButton.disabled = false;
        } catch (error) {
          showError(errorMessage, error.message);
          saveButton.disabled = false;
        }
        return;
      }

      const deleteButton = event.target.closest("[data-delete-tracker-group]");
      if (!deleteButton || deleteButton.disabled) return;
      const count = Number.parseInt(row.dataset.groupCount || "0", 10);
      pendingDelete = { id: groupId, name: groupName, count };
      clearError(deleteError);
      confirmationInput.value = "";
      confirmationField.hidden = count === 0;
      confirmationInput.required = count > 0;
      confirmationName.textContent = `"${groupName}"`;
      confirmDelete.disabled = count > 0;
      deleteMessage.textContent = count > 0
        ? `This permanently deletes ${count} ${count === 1 ? "job" : "jobs"} and all Job Tracker history in ${groupName}.`
        : `Delete the empty group ${groupName}?`;
      deleteDialog.showModal();
      if (count > 0) confirmationInput.focus();
    });

    confirmationInput.addEventListener("input", () => {
      if (!pendingDelete || pendingDelete.count === 0) return;
      confirmDelete.disabled = confirmationInput.value !== pendingDelete.name;
    });

    deleteDialog.addEventListener("close", () => {
      pendingDelete = null;
      confirmationInput.value = "";
      clearError(deleteError);
    });

    confirmDelete.addEventListener("click", async () => {
      if (!pendingDelete || confirmDelete.disabled) return;
      confirmDelete.disabled = true;
      clearError(deleteError);
      try {
        const deletedGroup = pendingDelete;
        await requestGroupChange(
          `/api/tracker-groups/${encodeURIComponent(pendingDelete.id)}`,
          "DELETE",
          {
            confirmation_name: pendingDelete.count > 0
              ? confirmationInput.value
              : null,
          },
          "Could not delete this group.",
        );
        removeGroupFromPage(deletedGroup.id);
        deleteDialog.close("deleted");
      } catch (error) {
        showError(deleteError, error.message);
        confirmDelete.disabled = (
          pendingDelete.count > 0
          && confirmationInput.value !== pendingDelete.name
        );
      }
    });
  };

  const renderJobReevaluationState = (progress, state) => {
    if (!progress) return;
    const stepText = typeof state?.step === "string" ? ` (${state.step})` : "";
    progress.hidden = false;
    progress.textContent = state?.message
      ? `${state.message}${stepText}`
      : `Re-evaluation in progress${stepText}`;
    progress.classList.toggle("is-error", state?.status === "failed");
  };

  const pollJobReevaluation = async (importId, progress) => {
    while (true) {
      await new Promise((resolve) => window.setTimeout(resolve, 650));
      let response;
      try {
        response = await fetch(
          `/api/manual-job-imports/${encodeURIComponent(importId)}`,
          {
            credentials: "same-origin",
            signal: AbortSignal.timeout(10_000),
          },
        );
      } catch (_error) {
        throw new Error(
          "Connection to re-evaluation progress lost. Try again.",
        );
      }
      if (response.status === 404) {
        throw new Error(
          "Re-evaluation state was lost after the service restarted. Try again.",
        );
      }
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not read re-evaluation progress.",
        ));
      }
      const state = await response.json();
      renderJobReevaluationState(progress, state);
      if (state.status === "complete") return state;
      if (state.status === "failed") return state;
    }
  };

  const initializeManualJobImport = () => {
    const dialog = document.querySelector("#manual-job-dialog");
    const opener = document.querySelector("[data-open-manual-job]");
    const form = dialog?.querySelector("[data-manual-job-form]");
    const urlInput = dialog?.querySelector("#manual-job-url");
    const resumeInput = dialog?.querySelector("#manual-job-resume");
    const errorMessage = dialog?.querySelector("[data-manual-job-error]");
    const progressMessage = dialog?.querySelector("[data-manual-job-progress]");
    const closeButton = dialog?.querySelector("[data-close-manual-job]");
    const submitButton = dialog?.querySelector("[data-submit-manual-job]");
    if (!dialog || !opener || !form || !urlInput || !errorMessage || !submitButton) {
      return;
    }
    let importing = false;
    const resetManualImportButton = () => {
      importing = false;
      form.setAttribute("aria-busy", "false");
      opener.disabled = false;
      closeButton?.removeAttribute("disabled");
      submitButton.disabled = false;
      resumeInput?.removeAttribute("disabled");
      submitButton.textContent = submitButton.dataset.defaultLabel || "Import to Saved";
      if (progressMessage) {
        progressMessage.hidden = true;
        progressMessage.classList.remove("manual-job-error");
      }
    };

    const renderManualImportState = (state) => {
      if (!progressMessage) return;
      const stepText = typeof state?.step === "string" ? ` (${state.step})` : "";
      progressMessage.hidden = false;
      progressMessage.textContent = state?.message
        ? `${state.message}${stepText}`
        : `Manual import in progress${stepText}`;
      if (state?.status === "failed") {
        progressMessage.classList.add("manual-job-error");
      } else {
        progressMessage.classList.remove("manual-job-error");
      }
    };

    const companySizeCheckFinished = (root, jobKey) => {
      const card = reviewCardForJob(root, jobKey);
      const provenance = card?.querySelector(
        ".company-size [data-manual-fact-provenance]",
      );
      return provenance?.textContent.includes("Checked ") || false;
    };

    const markCompanySizeChecking = (jobKey) => {
      const globalBlock = document.querySelector('[data-review-block="global"]');
      const card = reviewCardForJob(globalBlock || document, jobKey);
      const value = card?.querySelector(
        ".company-size [data-manual-fact-value]",
      );
      if (!value || value.textContent.trim() !== "Unknown") return false;
      value.replaceChildren(document.createTextNode("Checking..."));
      const provenance = card.querySelector(
        ".company-size [data-manual-fact-provenance]",
      );
      if (provenance) provenance.textContent = "";
      const search = card.querySelector("[data-company-size-search]");
      if (search) search.disabled = true;
      return true;
    };

    const pollImportedCompanySize = async (jobKey) => {
      for (let attempt = 0; attempt < 300; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 650));
        try {
          const refreshedDocument = await fetchReviewDocument(window.location.href);
          const refreshedBlock = refreshedDocument.querySelector(
            '[data-review-block="global"]',
          );
          if (!companySizeCheckFinished(refreshedBlock || refreshedDocument, jobKey)) {
            continue;
          }
          reconcileGlobalJobs(refreshedDocument);
          return;
        } catch (_error) {
          return;
        }
      }
    };

    const finalizeManualImport = async (state) => {
      const destination = new URL(window.location.href);
      const refreshedDocument = await fetchReviewDocument(destination.href);
      reconcileGlobalJobs(refreshedDocument);
      const jobKey = typeof state?.job_key === "string" ? state.job_key : "";
      const refreshedBlock = refreshedDocument.querySelector(
        '[data-review-block="global"]',
      );
      if (
        jobKey
        && !companySizeCheckFinished(refreshedBlock || refreshedDocument, jobKey)
        && markCompanySizeChecking(jobKey)
      ) {
        void pollImportedCompanySize(jobKey);
      }
      resetManualImportButton();
      dialog.close();
    };

    const pollManualImport = async (importId) => {
      while (true) {
        await new Promise((resolve) => window.setTimeout(resolve, 650));
        let response;
        try {
          response = await fetch(
            `/api/manual-job-imports/${encodeURIComponent(importId)}`,
            {
              credentials: "same-origin",
              signal: AbortSignal.timeout(10_000),
            },
          );
        } catch (_error) {
          throw new Error(
            "Connection to manual import progress lost. Return to Job Tracker and try again.",
          );
        }
        if (response.status === 404) {
          throw new Error(
            "Manual import state was lost after the service restarted. Return to Job Tracker and try again.",
          );
        }
        if (!response.ok) {
          throw new Error(await responseError(response));
        }
        const state = await response.json();
        renderManualImportState(state);
        if (state.status === "complete") {
          await finalizeManualImport(state);
          return;
        }
        if (state.status === "failed") {
          throw new Error(state.error || state.message || "Could not import this job page.");
        }
      }
    };

    opener.addEventListener("click", () => {
      errorMessage.hidden = true;
      errorMessage.textContent = "";
      if (progressMessage) {
        progressMessage.hidden = true;
        progressMessage.classList.remove("manual-job-error");
      }
      dialog.showModal();
      urlInput.focus();
    });
    closeButton?.addEventListener("click", () => {
      if (!importing) dialog.close();
    });
    dialog.addEventListener("cancel", (event) => {
      if (importing) event.preventDefault();
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (importing || !form.reportValidity()) return;
      importing = true;
      form.setAttribute("aria-busy", "true");
      opener.disabled = true;
      closeButton?.setAttribute("disabled", "");
      submitButton.disabled = true;
      resumeInput?.setAttribute("disabled", "");
      submitButton.textContent = "Importing...";
      errorMessage.hidden = true;
      if (progressMessage) {
        progressMessage.hidden = false;
        progressMessage.textContent = "Manual import started.";
        progressMessage.classList.remove("manual-job-error");
      }
      try {
        const data = new FormData();
        data.append("url", urlInput.value);
        data.append("resume", resumeInput.files[0]);
        const response = await fetch("/api/global-jobs/import-with-resume", {
          method: "POST",
          credentials: "same-origin",
          body: data,
        });
        if (!response.ok) {
          const message = await responseError(response);
          throw new Error(message);
        }
        const state = await response.json();
        if (!state?.import_id) {
          throw new Error("Manual import did not return a tracking ID.");
        }
        renderManualImportState(state);
        if (state.status === "complete") {
          await finalizeManualImport(state);
          return;
        }
        if (state.status === "failed") {
          throw new Error(state.error || state.message || "Could not import this job page.");
        }
        await pollManualImport(state.import_id);
      } catch (error) {
        errorMessage.textContent = error.message || "Could not import this job page.";
        errorMessage.hidden = false;
        resetManualImportButton();
      }
    });
  };

  const initializeReview = () => {
    document.querySelectorAll("[data-review-workspace]").forEach(
      initializeReviewGroupWorkspace,
    );
    jobBatchController = initializeJobBatchMode();
    initializeSourceFilter();
    userTagController = initializeUserTags();
    initializeGlobalSourceFilter();
    initializeGlobalSort();
    initializeManualJobImport();
    initializeTrackerGroupSettings();
    updateReviewGroupNoticeCounts();
    document.addEventListener(
      "job-scan:review-updated",
      updateReviewGroupNoticeCounts,
    );
    // A background re-evaluation restored after a page refresh has finished;
    // pull the job's new score and notice into the visible card.
    document.addEventListener(
      "job-scan:background-reevaluation-finished",
      (event) => {
        const jobKey = event.detail?.jobKey;
        if (!jobKey) return;
        void refreshReviewJob(jobKey, { preserveOpenDetail: true }).catch(
          () => {},
        );
      },
    );
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeReview, { once: true });
  } else {
    initializeReview();
  }

  const openJobDetail = (card) => {
    const dialog = card.querySelector("[data-job-detail-dialog]");
    if (dialog && !dialog.open) dialog.showModal();
    const finishedAt = card.dataset.reevaluationFinishedAt;
    if (!card.dataset.reevaluationStatus || !finishedAt) return;
    const jobKey = card.dataset.jobKey;
    void fetch(
      `/api/global-jobs/${encodeURIComponent(jobKey)}`
      + "/re-evaluation-result/acknowledge",
      {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ finished_at: finishedAt }),
      },
    ).then(async (response) => {
      if (
        response.status === 409
        && response.headers.get("X-Job-Scan-Conflict")
          === "re-evaluation-result-changed"
      ) {
        await refreshReviewJob(jobKey, { preserveOpenDetail: true });
        return;
      }
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not acknowledge this re-evaluation result.",
        ));
      }
      if (card.dataset.reevaluationFinishedAt !== finishedAt) return;
      delete card.dataset.reevaluationStatus;
      delete card.dataset.reevaluationFinishedAt;
      card.setAttribute(
        "aria-label",
        card.getAttribute("aria-label").replace(
          /\. Re-evaluation (?:succeeded|failed); open to acknowledge$/,
          "",
        ),
      );
      updateReviewGroupNoticeCounts();
    }).catch((error) => window.alert(error.message));
  };

  const openJobNoteDialog = (trigger, note = null) => {
    const card = trigger.closest("[data-job-preview-card]");
    const dialog = card?.querySelector("[data-job-note-dialog]");
    const form = dialog?.querySelector("[data-job-note-form]");
    const input = form?.elements.namedItem("content");
    const noteId = form?.elements.namedItem("note_id");
    if (!dialog || !form || !input || !noteId) return;
    form.reset();
    noteId.value = note?.dataset.noteId || "";
    input.value = note?.querySelector("[data-job-note-content]")?.textContent || "";
    dialog.querySelector("[data-job-note-dialog-title]").textContent = (
      note ? "Edit note" : "Add note"
    );
    dialog.querySelector("[data-job-note-save]").textContent = (
      note ? "Save changes" : "Save note"
    );
    const errorMessage = dialog.querySelector("[data-job-note-error]");
    errorMessage.hidden = true;
    dialog.ariaLabel = note ? "Edit note" : "Add note";
    dialog.showModal();
    input.focus();
  };

  const updateManualFact = (card, fieldName, rawValue) => {
    const fact = card.querySelector(`[data-manual-fact="${fieldName}"]`);
    const valueNode = fact?.querySelector("[data-manual-fact-value]");
    if (!fact || !valueNode) return;
    const value = fieldName === "company_industry" ? rawValue.trim() : rawValue;
    valueNode.textContent = "";
    if (fieldName === "posted_at") {
      const time = document.createElement("time");
      time.dateTime = value;
      time.textContent = value;
      valueNode.append(time);
      card.dataset.postedAt = value;
      const preview = card.querySelector(".job-preview-posted");
      if (preview) {
        preview.textContent = "Posted: ";
        preview.append(time.cloneNode(true));
      }
    } else if (fieldName === "company_size") {
      const employeeCount = Number(value);
      valueNode.textContent = `${new Intl.NumberFormat("en-US").format(employeeCount)} employees`;
      card.dataset.companySizeMinimum = String(employeeCount);
      card.dataset.companySizeMaximum = String(employeeCount);
    } else {
      valueNode.textContent = value;
      card.dataset.companyIndustry = value;
    }
    const provenance = fact.querySelector("[data-manual-fact-provenance]");
    if (provenance) provenance.textContent = " · Manually added";
    document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
  };

  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-manual-fact-open]");
    if (trigger) {
      const detail = trigger.closest("[data-job-detail-dialog]");
      const fieldName = trigger.dataset.manualFactOpen;
      const dialog = detail?.querySelector(
        `[data-manual-fact-dialog][data-manual-fact-field="${fieldName}"]`,
      );
      if (!dialog) return;
      dialog.showModal();
      const input = dialog.querySelector('input[name="value"]');
      input?.focus();
      if (input?.type === "date" && typeof input.showPicker === "function") {
        try {
          input.showPicker();
        } catch {
          // Keep the visible date input available when showPicker is unavailable.
        }
      }
      return;
    }
    const cancel = event.target.closest("[data-manual-fact-cancel]");
    cancel?.closest("[data-manual-fact-dialog]")?.close();
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-manual-fact-form]");
    if (!form) return;
    event.preventDefault();
    const dialog = form.closest("[data-manual-fact-dialog]");
    const card = form.closest("[data-job-key]");
    const fieldName = form.dataset.manualFactField;
    const input = form.querySelector('input[name="value"]');
    const button = form.querySelector("[data-manual-fact-save]");
    const errorMessage = form.querySelector("[data-manual-fact-error]");
    const value = input.value;
    const payloadValue = fieldName === "company_size" ? Number(value) : value;
    button.disabled = true;
    errorMessage.hidden = true;
    try {
      const response = await fetch(
        `/api/global-jobs/${encodeURIComponent(card.dataset.jobKey)}/facts`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ [fieldName]: payloadValue }),
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not save this job detail.",
        ));
      }
      updateManualFact(card, fieldName, value);
      dialog.close();
      dialog.remove();
    } catch (error) {
      errorMessage.textContent = error.message;
      errorMessage.hidden = false;
      button.disabled = false;
    }
  });

  document.addEventListener("pointerdown", (event) => {
    const editor = event.target.closest(".lifecycle-date-editor");
    const input = editor?.querySelector("[data-lifecycle-date-input]");
    if (!input || input.disabled || typeof input.showPicker !== "function") return;
    try {
      input.showPicker();
      event.preventDefault();
    } catch {
      // Keep the browser's normal date-input behavior when showPicker is unavailable.
    }
  });

  document.addEventListener("dblclick", (event) => {
    const step = event.target.closest("[data-lifecycle-step]");
    if (!step || event.target.closest(".lifecycle-date-editor")) return;
    if (step.dataset.lifecycleStatus === "saved") {
      window.alert(
        `${step.dataset.lifecycleStatusLabel} is the lifecycle starting point and cannot be deleted.`,
      );
      return;
    }
    const card = step.closest("[data-job-key]");
    const dialog = card?.querySelector("[data-lifecycle-delete-dialog]");
    if (!dialog) return;
    dialog.dataset.lifecycleEventIndex = step.dataset.lifecycleEventIndex;
    dialog.querySelector("[data-lifecycle-delete-status]").textContent = (
      step.dataset.lifecycleStatusLabel
    );
    event.preventDefault();
    dialog.showModal();
  });

  document.addEventListener("click", (event) => {
    const addNoteButton = event.target.closest("[data-job-note-add]");
    if (addNoteButton) {
      openJobNoteDialog(addNoteButton);
      return;
    }

    const editNoteButton = event.target.closest("[data-job-note-edit]");
    if (editNoteButton) {
      openJobNoteDialog(editNoteButton, editNoteButton.closest("[data-job-note]"));
      return;
    }

    const deleteNoteButton = event.target.closest("[data-job-note-delete]");
    if (deleteNoteButton) {
      const card = deleteNoteButton.closest("[data-job-preview-card]");
      const dialog = card?.querySelector("[data-job-note-delete-dialog]");
      const note = deleteNoteButton.closest("[data-job-note]");
      if (!dialog || !note) return;
      dialog.dataset.noteId = note.dataset.noteId;
      dialog.showModal();
      return;
    }

    const cancelNoteButton = event.target.closest("[data-job-note-cancel]");
    if (cancelNoteButton) {
      cancelNoteButton.closest("[data-job-note-dialog]")?.close();
      return;
    }

    const replaceResumeButton = event.target.closest("[data-job-resume-replace]");
    if (replaceResumeButton) {
      replaceResumeButton
        .closest("form")
        ?.querySelector("[data-job-resume-input]")
        ?.click();
      return;
    }

    const closeButton = event.target.closest("[data-close-job-detail]");
    if (closeButton) {
      const dialog = closeButton.closest("[data-job-detail-dialog]");
      const card = dialog?.closest("[data-job-preview-card]");
      dialog?.close();
      card?.focus();
      return;
    }

    const detailDialog = event.target.closest("[data-job-detail-dialog]");
    if (detailDialog) {
      if (event.target === detailDialog) detailDialog.close();
      return;
    }

    const card = event.target.closest("[data-job-preview-card]");
    if (!card) return;
    if (event.target.closest("a, button, input, select, label, form, summary, details")) {
      return;
    }
    openJobDetail(card);
  });

  document.addEventListener("keydown", (event) => {
    const card = event.target.closest("[data-job-preview-card]");
    if (!card || event.target !== card || !["Enter", " "].includes(event.key)) return;
    event.preventDefault();
    openJobDetail(card);
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-job-note-form]");
    if (!form) return;
    event.preventDefault();
    const card = form.closest("[data-job-preview-card]");
    const dialog = form.closest("[data-job-note-dialog]");
    const button = form.querySelector("[data-job-note-save]");
    const errorMessage = form.querySelector("[data-job-note-error]");
    const rawJobKey = card.dataset.jobKey;
    const noteId = form.elements.namedItem("note_id").value;
    const content = form.elements.namedItem("content").value;
    button.disabled = true;
    errorMessage.hidden = true;
    try {
      const baseEndpoint = `/api/global-jobs/${encodeURIComponent(rawJobKey)}/notes`;
      const response = await fetch(
        noteId ? `${baseEndpoint}/${encodeURIComponent(noteId)}` : baseEndpoint,
        {
          method: noteId ? "PUT" : "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content }),
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not save this note."));
      }
      dialog.close();
      await refreshReviewJob(rawJobKey, { preserveOpenDetail: true });
    } catch (error) {
      errorMessage.textContent = error.message;
      errorMessage.hidden = false;
      button.disabled = false;
      if (!dialog.open) dialog.showModal();
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-job-note-delete-confirm]");
    if (!button || button.disabled) return;
    const dialog = button.closest("[data-job-note-delete-dialog]");
    const card = button.closest("[data-job-preview-card]");
    const rawJobKey = card.dataset.jobKey;
    const noteId = dialog.dataset.noteId;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/global-jobs/${encodeURIComponent(rawJobKey)}/notes/${encodeURIComponent(noteId)}`,
        {
          method: "DELETE",
          credentials: "same-origin",
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not delete this note."));
      }
      dialog.close();
      await refreshReviewJob(rawJobKey, { preserveOpenDetail: true });
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-job-action]");
    if (!form) return;
    event.preventDefault();
    const action = form.dataset.jobAction;
    const rawJobKey = form.dataset.jobKey;
    const jobKey = encodeURIComponent(rawJobKey);
    const runId = document.body.dataset.reviewRunId;
    const statusScope = form.closest("[data-status-scope]")?.dataset.statusScope;
    const snapshotAction = action === "snapshot-force" ? "snapshot" : action;
    const forceSuffix = action === "snapshot-force" ? "?force=1" : "";
    const endpoint = statusScope === "global" && (
      action === "status"
      || action === "salary"
      || action === "resume"
      || action === "snapshot"
      || action === "snapshot-force"
      || action === "re-evaluate"
    )
      ? `/api/global-jobs/${jobKey}/${snapshotAction}${forceSuffix}`
      : runId
        ? `/api/scan-history/${encodeURIComponent(runId)}/jobs/${jobKey}/${snapshotAction}${forceSuffix}`
        : `/api/jobs/${jobKey}/${snapshotAction}${forceSuffix}`;
    const button = form.querySelector(
      'button[type="submit"], [data-job-resume-replace]',
    );
    if (button?.disabled) return;
    const buttonText = button?.textContent;
    if (button) button.disabled = true;
    if (button && (action === "status" || action === "salary")) {
      button.classList.add("is-saving");
      button.textContent = "Saving...";
    }
    if (
      button
      && (
        action === "snapshot"
        || action === "snapshot-force"
        || action === "resume"
        || action === "re-evaluate"
      )
    ) {
      button.textContent = action === "resume"
        ? "Uploading..."
        : action === "re-evaluate"
          ? "Re-evaluating..."
          : "Generating...";
    }
    const reevaluationProgress = action === "re-evaluate"
      ? form.closest(".job-action-group")?.querySelector(
        "[data-job-reevaluate-progress]",
      )
      : null;
    const reevaluationCard = action === "re-evaluate"
      ? form.closest("[data-job-preview-card]")
      : null;
    const reevaluationBlock = reevaluationCard?.closest("[data-review-block]");
    reevaluationProgress?.classList.remove("is-error");
    try {
      let response = await fetch(endpoint, requestOptions(action, form));
      if (
        action === "re-evaluate"
        && response.status === 409
        && response.headers.get("X-Job-Scan-Conflict") === "resume-unchanged"
      ) {
        const message = await responseError(
          response,
          "The resume has not changed since the last evaluation.",
        );
        if (!window.confirm(`${message} Re-evaluate anyway?`)) {
          if (button) {
            button.disabled = false;
            button.textContent = buttonText;
          }
          return;
        }
        response = await fetch(`${endpoint}?force=1`, requestOptions(action, form));
      }
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not update this job."));
      }
      if (action === "re-evaluate") {
        const state = await response.json();
        if (!state?.import_id) {
          throw new Error("Re-evaluation did not return a tracking ID.");
        }
        renderJobReevaluationState(reevaluationProgress, state);
        if (state.status === "running") {
          reevaluationCard?.classList.add("is-reevaluating");
        }
        const terminalState = state.status === "running"
          ? await pollJobReevaluation(state.import_id, reevaluationProgress)
          : state;
        await refreshReviewJob(rawJobKey, { preserveOpenDetail: true });
        if (terminalState.status === "failed") {
          const liveCard = reevaluationBlock
            ? reviewCardForJob(reevaluationBlock, rawJobKey)
            : null;
          const liveProgress = liveCard?.querySelector(
            "[data-job-reevaluate-progress]",
          );
          renderJobReevaluationState(liveProgress, terminalState);
        }
        return;
      }
      await refreshReviewJob(rawJobKey, {
        preserveOpenDetail: action === "resume",
      });
    } catch (error) {
      reevaluationCard?.classList.remove("is-reevaluating");
      if (reevaluationProgress) {
        reevaluationProgress.hidden = false;
        reevaluationProgress.textContent = error.message;
        reevaluationProgress.classList.add("is-error");
      } else {
        window.alert(error.message);
      }
      if (button) {
        button.disabled = false;
        button.classList.remove("is-saving");
        button.textContent = buttonText;
      }
    }
  });

  document.addEventListener("change", async (event) => {
    const lifecycleDateInput = event.target.closest("[data-lifecycle-date-input]");
    if (lifecycleDateInput) {
      const lifecycle = lifecycleDateInput.closest("[data-job-lifecycle]");
      const card = lifecycleDateInput.closest("[data-job-key]");
      const eventIndex = lifecycleDateInput.dataset.lifecycleEventIndex;
      const eventNumber = Number(eventIndex);
      const changedOn = lifecycleDateInput.value;
      const previousValue = lifecycleDateInput.defaultValue;
      const matchingInputs = lifecycle.querySelectorAll(
        `[data-lifecycle-date-input][data-lifecycle-event-index="${eventIndex}"]`,
      );
      const adjacentDate = (index) => lifecycle.querySelector(
        `[data-lifecycle-date-input][data-lifecycle-event-index="${index}"]`,
      )?.value;
      const previousDate = eventNumber > 0
        ? adjacentDate(eventNumber - 1)
        : null;
      const nextDate = adjacentDate(eventNumber + 1);
      if (
        (previousDate && changedOn < previousDate)
        || (nextDate && changedOn > nextDate)
      ) {
        matchingInputs.forEach((input) => { input.value = previousValue; });
        window.alert("This date must stay between its adjacent lifecycle dates.");
        return;
      }
      matchingInputs.forEach((input) => { input.disabled = true; });
      try {
        const response = await fetch(
          `/api/global-jobs/${encodeURIComponent(card.dataset.jobKey)}/lifecycle/${eventIndex}/date`,
          {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ changed_on: changedOn }),
          },
        );
        if (!response.ok) {
          throw new Error(await responseError(
            response,
            "Could not update this lifecycle date.",
          ));
        }
        card.querySelectorAll(`[data-lifecycle-time="${eventIndex}"]`).forEach(
          (time) => {
            time.dateTime = `${changedOn}${time.dateTime.slice(10)}`;
            time.textContent = time.dataset.lifecycleTimeFormat === "datetime"
              ? `${changedOn}${time.textContent.slice(10)}`
              : changedOn;
          },
        );
        matchingInputs.forEach((input) => {
          input.value = changedOn;
          input.defaultValue = changedOn;
        });
        const appliedTime = card.querySelector(
          "[data-job-preview-applied] [data-lifecycle-time]",
        );
        if (appliedTime?.dataset.lifecycleTime === eventIndex) {
          card.dataset.appliedAt = appliedTime.dateTime;
          document.dispatchEvent(new CustomEvent("job-scan:review-updated"));
        }
      } catch (error) {
        matchingInputs.forEach((input) => { input.value = previousValue; });
        window.alert(error.message);
      } finally {
        matchingInputs.forEach((input) => { input.disabled = false; });
      }
      return;
    }

    const resumeInput = event.target.closest("[data-job-resume-input]");
    if (resumeInput) {
      resumeInput.form?.requestSubmit();
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-confirm-lifecycle-delete]");
    if (!button || button.disabled) return;
    const dialog = button.closest("[data-lifecycle-delete-dialog]");
    const card = dialog.closest("[data-job-key]");
    const rawJobKey = card.dataset.jobKey;
    const eventIndex = dialog.dataset.lifecycleEventIndex;
    button.disabled = true;
    try {
      const response = await fetch(
        `/api/global-jobs/${encodeURIComponent(rawJobKey)}/lifecycle/${eventIndex}`,
        {
          method: "DELETE",
          credentials: "same-origin",
        },
      );
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not delete this lifecycle node.",
        ));
      }
      dialog.close();
      await refreshJobLifecycle(card, rawJobKey);
      button.disabled = false;
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-global-job-delete]");
    if (!button) return;
    if (!window.confirm("Permanently delete this job and its Job Tracker history?")) return;
    const card = button.closest("[data-job-key]");
    const rawJobKey = card.dataset.jobKey;
    const jobKey = encodeURIComponent(rawJobKey);
    button.disabled = true;
    try {
      const response = await fetch(`/api/global-jobs/${jobKey}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not delete this job from Job Tracker.",
        ));
      }
      await refreshReviewJob(rawJobKey);
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-company-size-search]");
    if (!button || button.disabled) return;
    const card = button.closest("[data-job-key]");
    const errorMessage = card.querySelector(".company-size-search-error");
    const rawJobKey = card.dataset.jobKey;
    const jobKey = encodeURIComponent(rawJobKey);
    const runId = document.body.dataset.reviewRunId;
    const endpoint = card.dataset.statusScope === "global"
      ? `/api/global-jobs/${jobKey}/company-size`
      : runId
        ? `/api/scan-history/${encodeURIComponent(runId)}/jobs/${jobKey}/company-size`
        : `/api/jobs/${jobKey}/company-size`;
    button.disabled = true;
    button.textContent = "Searching...";
    errorMessage.hidden = true;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        credentials: "same-origin",
      });
      if (!response.ok) {
        throw new Error(await responseError(
          response,
          "Could not verify this company's employee count.",
        ));
      }
      await refreshReviewJob(rawJobKey);
    } catch (error) {
      errorMessage.textContent = error.message;
      errorMessage.hidden = false;
      button.disabled = false;
      button.textContent = "AI Search";
    }
  });

})();
