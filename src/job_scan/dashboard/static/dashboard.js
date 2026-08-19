(() => {
  const requestOptions = (action, form) => {
    const options = {
      method: "POST",
      credentials: "same-origin",
    };
    if (action === "restore") {
      return options;
    }
    const status = form.elements.namedItem("status").value;
    return {
      ...options,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status }),
    };
  };

  const sourceFilterKey = "job-scan.review-source-filter.v1";

  const cardSources = (card) => card.dataset.sources.split(",").filter(Boolean);

  const restoredSourceFilter = (sources) => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(sourceFilterKey));
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

  const saveSourceFilter = (sources, selected) => {
    try {
      window.localStorage.setItem(
        sourceFilterKey,
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

  const applyReviewFilters = (
    cards,
    values,
    postedWithinDays,
    minimumScore,
    companySizeMinimum,
    companyIndustry,
    languageRequirement,
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
        !matchesLanguageRequirement(card, languageRequirement);
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

    const labels = {
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
    const selectedSources = new Set(restoredSourceFilter(sources));
    sources.forEach((source) => {
      const option = document.createElement("option");
      option.value = source;
      option.textContent = labels[source] || source;
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

  const reviewGroupOrderKey = "job-scan.review-group-order.v1";

  const restoredReviewGroupOrder = (storageKey, availableIds) => {
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey));
      if (!Array.isArray(saved)) return availableIds;
      const available = new Set(availableIds);
      const restored = saved.filter(
        (id, index) =>
          typeof id === "string" &&
          available.has(id) &&
          saved.indexOf(id) === index,
      );
      return [
        ...restored,
        ...availableIds.filter((id) => !restored.includes(id)),
      ];
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
    let touchPointerId = null;
    let touchDropTarget = null;
    let touchDropBefore = false;
    const clearDragState = () => {
      reviewGroupTabs(navigation).forEach((tab) => {
        tab.classList.remove(
          "is-dragging",
          "is-drag-over-before",
          "is-drag-over-after",
        );
      });
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
    navigation.addEventListener("keydown", (event) => {
      if (!event.altKey || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      const tab = dropTarget(event);
      if (!tab) return;
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
    navigation.addEventListener("drop", (event) => {
      const target = dropTarget(event);
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

  const reconcileReviewJob = (refreshedDocument, jobKey) => {
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

  const refreshReviewJob = async (jobKey) => {
    const refreshedDocument = await fetchReviewDocument(window.location.href);
    reconcileReviewJob(refreshedDocument, jobKey);
  };

  const reconcileGlobalResumeSelection = (refreshedDocument) => {
    const liveBlock = document.querySelector('[data-review-block="global"]');
    const refreshedBlock = refreshedDocument.querySelector(
      '[data-review-block="global"]',
    );
    const liveResumeSection = liveBlock?.querySelector(".global-resume-section");
    const refreshedResumeSection = refreshedBlock?.querySelector(
      ".global-resume-section",
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
      || !liveResumeSection
      || !refreshedResumeSection
      || !liveGroups
      || !refreshedGroups
    ) {
      throw new Error("Could not refresh this resume's Global jobs.");
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
    liveResumeSection.replaceWith(
      document.importNode(refreshedResumeSection, true),
    );
    document.body.dataset.selectedResumeId =
      refreshedDocument.body.dataset.selectedResumeId || "";
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

  const initializeManualJobImport = () => {
    const dialog = document.querySelector("#manual-job-dialog");
    const opener = document.querySelector("[data-open-manual-job]");
    const form = dialog?.querySelector("[data-manual-job-form]");
    const urlInput = dialog?.querySelector("#manual-job-url");
    const resumeInput = dialog?.querySelector("#manual-job-resume");
    const errorMessage = dialog?.querySelector("[data-manual-job-error]");
    const closeButton = dialog?.querySelector("[data-close-manual-job]");
    const submitButton = dialog?.querySelector("[data-submit-manual-job]");
    if (!dialog || !opener || !form || !urlInput || !errorMessage || !submitButton) {
      return;
    }
    let importing = false;
    opener.addEventListener("click", () => {
      errorMessage.hidden = true;
      errorMessage.textContent = "";
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
      submitButton.textContent = "Importing...";
      errorMessage.hidden = true;
      try {
        const uploadedResume = resumeInput?.files?.[0];
        let endpoint = "/api/global-jobs/import";
        let options;
        if (uploadedResume) {
          const data = new FormData();
          data.append("url", urlInput.value);
          data.append("resume", uploadedResume);
          endpoint = "/api/global-jobs/import-with-resume";
          options = {
            method: "POST",
            credentials: "same-origin",
            body: data,
          };
        } else {
          const payload = { url: urlInput.value };
          const resumeId = document.body.dataset.selectedResumeId?.trim();
          if (resumeId) payload.resume_id = resumeId;
          options = {
            method: "POST",
            credentials: "same-origin",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          };
        }
        const response = await fetch(endpoint, options);
        if (!response.ok) {
          let message = `Request failed (${response.status}).`;
          try {
            const payload = await response.json();
            if (typeof payload.detail === "string") message = payload.detail;
          } catch (_error) {
            // Keep the status fallback when the response is not JSON.
          }
          throw new Error(message);
        }
        let imported = null;
        try {
          imported = await response.json();
        } catch (_error) {
          // Older local responses can still reload the current selection.
        }
        if (typeof imported?.resume_id === "string") {
          const destination = new URL(window.location.href);
          destination.searchParams.set("resume_id", imported.resume_id);
          destination.hash = "review";
          window.location.assign(destination.toString());
        } else {
          window.location.reload();
        }
      } catch (error) {
        errorMessage.textContent = error.message || "Could not import this job page.";
        errorMessage.hidden = false;
        importing = false;
        form.setAttribute("aria-busy", "false");
        opener.disabled = false;
        closeButton?.removeAttribute("disabled");
        submitButton.disabled = false;
        submitButton.textContent = "Import to Shortlisted";
      }
    });
  };

  const initializeReview = () => {
    document.querySelectorAll("[data-review-workspace]").forEach(
      initializeReviewGroupWorkspace,
    );
    initializeSourceFilter();
    initializeManualJobImport();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initializeReview, { once: true });
  } else {
    initializeReview();
  }

  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("form[data-job-action]");
    if (!form) return;
    event.preventDefault();
    const action = form.dataset.jobAction;
    const rawJobKey = form.dataset.jobKey;
    const jobKey = encodeURIComponent(rawJobKey);
    const runId = document.body.dataset.reviewRunId;
    const statusScope = form.closest("[data-status-scope]")?.dataset.statusScope;
    const endpoint = action === "status" && statusScope === "global"
      ? `/api/global-jobs/${jobKey}/status`
      : runId
        ? `/api/scan-history/${encodeURIComponent(runId)}/jobs/${jobKey}/${action}`
        : `/api/jobs/${jobKey}/${action}`;
    const button = form.querySelector('button[type="submit"]');
    if (button?.disabled) return;
    if (button) button.disabled = true;
    try {
      const response = await fetch(endpoint, requestOptions(action, form));
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not update this job."));
      }
      await refreshReviewJob(rawJobKey);
    } catch (error) {
      window.alert(error.message);
      if (button) button.disabled = false;
    }
  });

  let globalResumeRequestVersion = 0;
  document.addEventListener("change", async (event) => {
    const select = event.target.closest("[data-global-resume-select]");
    if (!select || select.disabled) return;
    const selectedOption = select.selectedOptions[0];
    const destinationUrl = selectedOption?.dataset.reviewUrl;
    if (!destinationUrl) return;
    if (selectedOption.dataset.globalResumeId === document.body.dataset.selectedResumeId) {
      return;
    }

    const requestVersion = ++globalResumeRequestVersion;
    const resumeSection = select.closest(".global-resume-section");
    const destination = new URL(destinationUrl, window.location.href);
    destination.hash = window.location.hash || destination.hash;
    select.disabled = true;
    resumeSection?.setAttribute("aria-busy", "true");
    try {
      const refreshedDocument = await fetchReviewDocument(destination.href);
      if (requestVersion !== globalResumeRequestVersion) return;
      reconcileGlobalResumeSelection(refreshedDocument);
      window.history.replaceState(null, "", destination.href);
    } catch (error) {
      if (requestVersion === globalResumeRequestVersion) {
        select.value = document.body.dataset.selectedResumeId || "";
        window.alert(error.message);
      }
    } finally {
      if (requestVersion === globalResumeRequestVersion) {
        select.disabled = false;
        resumeSection?.removeAttribute("aria-busy");
      }
    }
  });

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-global-job-delete]");
    if (!button) return;
    if (!window.confirm("Delete this job from Global job status?")) return;
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
          "Could not delete this job from Global job status.",
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
