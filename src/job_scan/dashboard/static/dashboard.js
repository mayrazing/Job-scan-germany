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
    document.querySelectorAll(".review-groups > .job-group").forEach((group) => {
      const visibleCount = [...group.querySelectorAll(".job-card")].filter(
        (card) => !card.hidden,
      ).length;
      document
        .querySelectorAll(`[data-review-group-count="${group.id}"]`)
        .forEach((count) => {
          count.textContent = String(visibleCount);
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
    const cards = [
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
          cards,
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
        cards,
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

  const initializeManualJobImport = () => {
    const dialog = document.querySelector("#manual-job-dialog");
    const opener = document.querySelector("[data-open-manual-job]");
    const form = dialog?.querySelector("[data-manual-job-form]");
    const urlInput = dialog?.querySelector("#manual-job-url");
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
        const payload = { url: urlInput.value };
        const runId = document.body.dataset.reviewRunId?.trim();
        if (runId) payload.run_id = runId;
        const response = await fetch("/api/global-jobs/import", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
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
        window.location.reload();
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

  document.querySelectorAll("form[data-job-action]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const action = form.dataset.jobAction;
      const jobKey = encodeURIComponent(form.dataset.jobKey);
      const runId = document.body.dataset.reviewRunId;
      const statusScope = form.closest("[data-status-scope]")?.dataset.statusScope;
      const endpoint = action === "status" && statusScope === "global"
        ? `/api/global-jobs/${jobKey}/status`
        : runId
          ? `/api/scan-history/${encodeURIComponent(runId)}/jobs/${jobKey}/${action}`
          : `/api/jobs/${jobKey}/${action}`;
      const response = await fetch(
        endpoint,
        requestOptions(action, form),
      );
      if (response.ok) {
        window.location.reload();
      }
    });
  });

  document.querySelectorAll("[data-global-job-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Delete this job from Global job status?")) return;
      const card = button.closest("[data-job-key]");
      const jobKey = encodeURIComponent(card.dataset.jobKey);
      button.disabled = true;
      try {
        const response = await fetch(`/api/global-jobs/${jobKey}`, {
          method: "DELETE",
          credentials: "same-origin",
        });
        if (!response.ok) {
          let message = "Could not delete this job from Global job status.";
          try {
            const payload = await response.json();
            if (typeof payload.detail === "string") message = payload.detail;
          } catch (_error) {
            // Keep the short fallback when the server response is not JSON.
          }
          throw new Error(message);
        }
        window.location.reload();
      } catch (error) {
        window.alert(error.message);
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-company-size-search]").forEach((button) => {
    button.addEventListener("click", async () => {
      const card = button.closest("[data-job-key]");
      const errorMessage = card.querySelector(".company-size-search-error");
      const jobKey = encodeURIComponent(card.dataset.jobKey);
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
          let message = "Could not verify this company's employee count.";
          try {
            const payload = await response.json();
            if (typeof payload.detail === "string") message = payload.detail;
          } catch (_error) {
            // Keep the short fallback when the server response is not JSON.
          }
          throw new Error(message);
        }
        window.location.reload();
      } catch (error) {
        errorMessage.textContent = error.message;
        errorMessage.hidden = false;
        button.disabled = false;
        button.textContent = "AI Search";
      }
    });
  });

})();
