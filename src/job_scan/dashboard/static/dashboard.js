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

  const cardSources = (card) => card.dataset.sources.split(",").filter(Boolean);

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
        const visibleCount = [...group.querySelectorAll(".job-card")].filter(
          (card) => !card.hidden,
        ).length;
        workspace
          .querySelectorAll(`[data-review-group-count="${group.id}"]`)
          .forEach((count) => {
            count.textContent = String(visibleCount);
          });
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
  ) => {
    const selected = new Set(values);
    cards.forEach((card) => {
      const matchesSource = cardSources(card).some((source) => selected.has(source));
      card.hidden =
        !matchesSource ||
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
    sourceFilter.hidden = sources.length === 0;

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
      sourceFilter.hidden = sources.length === 0;
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
    document.addEventListener("job-scan:review-updated", applyGlobalFilters);
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

    let draggedTab = null;
    let draggedJobCard = null;
    const jobDragPreviewScale = 0.6;
    let touchPointerId = null;
    let touchDropTarget = null;
    let touchDropBefore = false;
    let lastTouchJumpTab = null;
    let lastTouchJumpAt = 0;
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
      const latest = [...group.querySelectorAll(
        ':scope > .card-grid > .job-card[data-reevaluation-status]',
      )].sort((left, right) => (
        Date.parse(right.dataset.reevaluationFinishedAt || "")
        - Date.parse(left.dataset.reevaluationFinishedAt || "")
      ))[0];
      if (!latest) return false;
      selectReviewGroup(container, navigation, groupId, true);
      revealFilteredReevaluationCard(latest);
      latest.focus({ preventScroll: true });
      latest.scrollIntoView({ behavior: "smooth", block: "center" });
      return true;
    };

    navigation.addEventListener("click", (event) => {
      const tab = dropTarget(event);
      if (!tab) return;
      event.preventDefault();
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
      card.classList.add("is-job-dragging");
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

  const refreshOpenJobCard = (liveCard, refreshedCard, selectedForAts) => {
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
    const atsCheckbox = liveCard.querySelector("[data-ats-select-job]");
    if (atsCheckbox) atsCheckbox.checked = selectedForAts;
    return true;
  };

  const reconcileReviewJob = (
    refreshedDocument,
    jobKey,
    { preserveOpenDetail = false } = {},
  ) => {
    const selectedForAts = [
      ...document.querySelectorAll("article.job-card[data-job-key]"),
    ].some(
      (card) =>
        card.dataset.jobKey === jobKey
        && card.querySelector("[data-ats-select-job]")?.checked,
    );
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
        && refreshOpenJobCard(liveCard, refreshedCard, selectedForAts)
      ) {
        return;
      }
      const replacement = document.importNode(refreshedCard, true);
      const replacementAts = replacement.querySelector("[data-ats-select-job]");
      if (replacementAts) replacementAts.checked = selectedForAts;
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

  const refreshJobLifecycle = async (card, jobKey) => {
    const refreshedDocument = await fetchReviewDocument(window.location.href);
    const blockName = card.closest("[data-review-block]")?.dataset.reviewBlock;
    const refreshedBlock = [
      ...refreshedDocument.querySelectorAll("[data-review-block]"),
    ].find((block) => block.dataset.reviewBlock === blockName);
    const liveLifecycle = card.querySelector("[data-job-lifecycle]");
    const refreshedLifecycle = reviewCardForJob(
      refreshedBlock || refreshedDocument,
      jobKey,
    )?.querySelector("[data-job-lifecycle]");
    if (!liveLifecycle || !refreshedLifecycle) {
      throw new Error("Could not refresh this job lifecycle.");
    }
    liveLifecycle.replaceWith(document.importNode(refreshedLifecycle, true));
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

    const selectedForAts = new Set(
      [...liveBlock.querySelectorAll("[data-ats-select-job]:checked")]
        .map((checkbox) => checkbox.closest("[data-job-key]")?.dataset.jobKey)
        .filter(Boolean),
    );
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
      replacement.querySelectorAll("[data-ats-select-job]").forEach((checkbox) => {
        const jobKey = checkbox.closest("[data-job-key]")?.dataset.jobKey;
        checkbox.checked = selectedForAts.has(jobKey);
      });
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
      submitButton.textContent = "Import to Saved";
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
    initializeSourceFilter();
    initializeGlobalSourceFilter();
    initializeManualJobImport();
    updateReviewGroupNoticeCounts();
    document.addEventListener(
      "job-scan:review-updated",
      updateReviewGroupNoticeCounts,
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
      window.alert("Saved is the lifecycle starting point and cannot be deleted.");
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
