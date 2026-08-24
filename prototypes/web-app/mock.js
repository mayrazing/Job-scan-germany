(() => {
  const form = document.querySelector("#setup-form");
  const setupView = document.querySelector("#setup");
  const runView = document.querySelector("#run-view");
  const reviewView = document.querySelector("#review-preview");
  const atsRunningView = document.querySelector("#ats-running");
  const atsView = document.querySelector("#ats-check");
  const reviewLink = document.querySelector("#review-link");
  const headerStatus = document.querySelector("#header-status");
  const formError = document.querySelector("#form-error");
  const progress = document.querySelector("[aria-label='Scan progress']");
  const runPercent = document.querySelector("#run-percent");
  const runMessage = document.querySelector("#run-message");
  const runSummary = document.querySelector("#run-summary");
  const scanTime = document.querySelector("#scan-time");
  const scheduleStatus = document.querySelector("#schedule-status");
  const scheduleNote = document.querySelector("#schedule-note");
  const removeSchedule = document.querySelector("#remove-schedule");
  const aiProviderList = document.querySelector(".ai-provider-list");
  const aiProviderEditor = document.querySelector("#ai-provider-editor");
  const aiProviderName = document.querySelector("#ai-provider-name");
  const aiProviderBaseUrl = document.querySelector("#ai-provider-base-url");
  const aiProviderApiKey = document.querySelector("#ai-provider-api-key");
  const aiProviderModel = document.querySelector("#ai-provider-model");
  const aiProviderEffort = document.querySelector("#ai-provider-effort");
  const aiEditorFeedback = document.querySelector("#ai-editor-feedback");
  const aiEditorMode = document.querySelector("#ai-editor-mode");
  const aiEditorTitle = document.querySelector("#ai-editor-title");
  const aiRuntime = document.querySelector("#ai-runtime");
  const claudeCodeSettings = document.querySelector("#claude-code-settings");
  const apiModelSettings = document.querySelector("#api-model-settings");
  const apiModelSummary = document.querySelector("#api-model-summary");
  const atsRunProgress = document.querySelector("#ats-run-progress");
  const atsRunPercent = document.querySelector("#ats-run-percent");
  const atsRunMessage = document.querySelector("#ats-run-message");
  const atsRunBadge = document.querySelector("#ats-run-badge");
  const atsResultsLink = document.querySelector("#ats-results-link");
  const atsHistory = document.querySelector("#ats-history");
  const atsHistoryContext = document.querySelector("#ats-history-context");
  const atsResultScope = document.querySelector("#ats-result-scope");
  const resumeReadinessScore = document.querySelector("#resume-readiness-score");
  const atsStartButton = document.querySelector("[data-open-ats]");
  const atsTaskList = document.querySelector(".ats-run-log");
  const atsRunningProgressTitle = document.querySelector("#ats-running-progress-title");
  const timers = [];
  const atsTimers = [];
  let scheduledTime = "";
  let editingProviderId = "deepseek";
  let atsStarted = false;
  let atsComplete = false;

  const aiProviders = [
    {
      id: "deepseek",
      name: "DeepSeek",
      baseUrl: "https://api.deepseek.com/anthropic",
      model: "deepseek-v4-flash",
      effort: "low",
    },
  ];

  const placeholders = {
    "german-level": "Search or type a level",
    "search-terms": "Search or add a role",
    locations: "Search German cities",
    "claude-model": "Search or type a model",
  };

  const steps = [
    { key: "profile", percent: 22, message: "Profile created from resume.", result: "Profile ready" },
    { key: "sources", percent: 57, message: "Searching configured keyword sources...", result: "142 jobs found" },
    { key: "review", percent: 88, message: "Reviewing complete job descriptions with Claude...", result: "24 jobs reviewed" },
    { key: "publish", percent: 100, message: "Review queue published.", result: "9 eligible jobs" },
  ];

  const selectItems = (control) => [
    ...control.querySelectorAll("ui5-option, ui5-cb-item, ui5-mcb-item"),
  ];

  const createSelectItem = (tagName, value, label = value) => {
    const item = document.createElement(tagName);
    item.value = value;
    item.setAttribute("text", label);
    item.textContent = label;
    return item;
  };

  const initializeSearchSelects = (root = document) => {
    root.querySelectorAll("ui5-combobox[data-search-select], ui5-multi-combobox[data-search-select]").forEach((control) => {
      control.placeholder = placeholders[control.id] || "Search or type a value";
      if (control.dataset.create !== "true" || control.localName !== "ui5-multi-combobox") return;
      control.addEventListener("change", () => {
        const customValue = control.value.trim();
        if (!customValue) return;
        if (!selectItems(control).some((item) => item.value === customValue)) {
          control.append(createSelectItem("ui5-mcb-item", customValue));
        }
        control.selectedValues = [...new Set([...control.selectedValues, customValue])];
        control.value = "";
      });
    });
  };

  const providerInitials = (name) => name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase() || "AI";

  const createProviderButton = (label, attribute) => {
    const button = document.createElement("ui5-button");
    button.className = "btn btn-outline-secondary";
    button.type = "Button";
    button.setAttribute("data-ui5-adapted", "");
    button.textContent = label;
    button.toggleAttribute(attribute, true);
    return button;
  };

  const renderAiProviders = () => {
    aiProviderList.replaceChildren();
    aiProviders.forEach((provider) => {
      const row = document.createElement("article");
      row.className = "ai-provider-row";
      row.dataset.aiProvider = provider.id;

      const mark = document.createElement("div");
      mark.className = "ai-provider-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = providerInitials(provider.name);

      const copy = document.createElement("div");
      copy.className = "ai-provider-copy";
      const title = document.createElement("div");
      title.className = "ai-provider-title";
      const name = document.createElement("strong");
      name.textContent = provider.name;
      title.append(name);
      const summary = document.createElement("p");
      summary.textContent = `${provider.model} · Effort: ${provider.effort}`;
      const url = document.createElement("small");
      url.textContent = provider.baseUrl;
      copy.append(title, summary, url);

      const actions = document.createElement("div");
      actions.className = "ai-provider-actions";
      actions.append(createProviderButton("Edit", "data-edit-ai-provider"));
      row.append(mark, copy, actions);
      aiProviderList.append(row);
    });
  };

  const selectedAiProvider = () => {
    if (!aiRuntime.value.startsWith("api:")) return null;
    const providerId = aiRuntime.value.slice("api:".length);
    return aiProviders.find((provider) => provider.id === providerId) || null;
  };

  const syncAiRuntime = () => {
    const previousValue = aiRuntime.value;
    aiRuntime.querySelectorAll("ui5-option[value^='api:']").forEach((option) => option.remove());
    aiProviders.forEach((provider) => {
      aiRuntime.append(createSelectItem(
        "ui5-option",
        `api:${provider.id}`,
        `${provider.name} API · ${provider.model}`,
      ));
    });
    aiRuntime.value = selectItems(aiRuntime).some(
      (option) => option.value === previousValue,
    ) ? previousValue : "claude-code";

    const selected = selectedAiProvider();
    const useApi = selected !== null;
    claudeCodeSettings.hidden = Boolean(useApi);
    apiModelSettings.hidden = !useApi;
    if (selected) {
      apiModelSummary.textContent = `${selected.name} · ${selected.model}`;
      apiModelSettings.querySelector(".ai-provider-mark").textContent = providerInitials(selected.name);
      apiModelSettings.querySelector(".form-text").textContent = `Effort: ${selected.effort} · Anthropic-compatible API`;
    }
    const runtimeName = selected ? `${selected.name} API` : "Claude Code";
    document.querySelector("[data-run-item='review'] strong").textContent = `${runtimeName} review`;
    steps[2].message = `Reviewing complete job descriptions with ${runtimeName}...`;
  };

  const setProviderModel = (value, options = []) => {
    aiProviderModel.replaceChildren();
    const values = [...new Set([value, ...options].filter(Boolean))];
    values.forEach((model) => aiProviderModel.append(createSelectItem("ui5-cb-item", model)));
    aiProviderModel.value = value;
  };

  const openAiEditor = (provider = null) => {
    editingProviderId = provider?.id || null;
    aiEditorMode.textContent = provider ? "Edit API model" : "New API model";
    aiEditorTitle.textContent = provider?.name || "Connect a provider";
    aiProviderName.value = provider?.name || "";
    aiProviderBaseUrl.value = provider?.baseUrl || "";
    aiProviderBaseUrl.valueState = "None";
    aiProviderApiKey.value = "";
    aiProviderApiKey.placeholder = provider ? "Configured · enter to replace" : "Required for a new configuration";
    setProviderModel(provider?.model || "");
    aiProviderEffort.value = provider?.effort || "low";
    aiEditorFeedback.textContent = "";
    aiEditorFeedback.removeAttribute("data-state");
    aiProviderEditor.hidden = false;
    aiProviderName.focus();
  };

  const closeAiEditor = () => {
    aiProviderEditor.hidden = true;
    aiEditorFeedback.textContent = "";
  };

  const isValidProviderBaseUrl = () => {
    try {
      const url = new URL(aiProviderBaseUrl.value.trim());
      return ["http:", "https:"].includes(url.protocol);
    } catch (_error) {
      return false;
    }
  };

  const renderSchedule = () => {
    const active = scheduledTime !== "";
    scheduleStatus.textContent = active ? `Every day at ${scheduledTime}` : "Not scheduled";
    scheduleNote.textContent = active
      ? "Automatic scan owned by job-scan on this computer."
      : "No automatic scans. Run manually anytime.";
    removeSchedule.disabled = !active;
  };

  const setView = (selected) => {
    setupView.hidden = selected !== "setup";
    runView.hidden = selected !== "run";
    reviewView.hidden = selected !== "review";
    atsRunningView.hidden = selected !== "ats-running";
    atsView.hidden = selected !== "ats";
    const activeStep = selected.startsWith("ats") ? "ats" : selected;
    document.querySelectorAll("[data-nav-step]").forEach((link) => {
      const active = link.dataset.navStep === activeStep;
      link.design = active ? "Emphasized" : "Default";
      link.toggleAttribute("data-active", active);
      if (active) link.setAttribute("aria-current", "step");
      else link.removeAttribute("aria-current");
    });
  };

  const setRailState = (activeKey) => {
    const keys = ["setup", "profile", "sources", "review", "ats"];
    const activeIndex = keys.indexOf(activeKey);
    document.querySelectorAll("[data-step]").forEach((item) => {
      const itemIndex = keys.indexOf(item.dataset.step);
      item.dataset.state = itemIndex < activeIndex ? "complete" : itemIndex === activeIndex ? "active" : "waiting";
    });
  };

  const resetRun = () => {
    timers.splice(0).forEach(window.clearTimeout);
    progress.value = 0;
    progress.displayValue = "0%";
    progress.setAttribute("aria-valuenow", "0");
    runPercent.textContent = "0%";
    runMessage.textContent = "Preparing your factual profile...";
    runSummary.hidden = true;
    reviewLink.hidden = true;
    document.querySelectorAll("[data-run-item]").forEach((item) => {
      item.dataset.state = "waiting";
      item.querySelector("small").textContent = "Waiting";
    });
  };

  const completeStep = (step, index) => {
    document.querySelectorAll("[data-run-item]").forEach((item, itemIndex) => {
      if (itemIndex < index) item.dataset.state = "complete";
      if (itemIndex === index) item.dataset.state = "active";
    });
    const item = document.querySelector(`[data-run-item="${step.key}"]`);
    item.querySelector("small").textContent = index === steps.length - 1 ? step.result : "Running";
    progress.value = step.percent;
    progress.displayValue = `${step.percent}%`;
    progress.setAttribute("aria-valuenow", String(step.percent));
    runPercent.textContent = `${step.percent}%`;
    runMessage.textContent = step.message;
    setRailState(step.key === "publish" ? "review" : step.key);

    if (index > 0) {
      const previous = document.querySelector(`[data-run-item="${steps[index - 1].key}"]`);
      previous.dataset.state = "complete";
      previous.querySelector("small").textContent = steps[index - 1].result;
    }

    if (index === steps.length - 1) {
      item.dataset.state = "complete";
      runSummary.hidden = false;
      reviewLink.hidden = false;
      headerStatus.textContent = "Review ready";
    }
  };

  const startMockRun = () => {
    resetRun();
    setView("run");
    headerStatus.textContent = "Running";
    steps.forEach((step, index) => {
      timers.push(window.setTimeout(() => completeStep(step, index), 550 + index * 850));
    });
  };

  const setAtsTaskState = (key, state, label) => {
    const item = document.querySelector(`[data-ats-task="${key}"]`);
    item.dataset.state = state;
    item.querySelector("[data-ats-task-status]").textContent = label;
  };

  const updateAtsProgress = (completed, total, message) => {
    const percent = Math.round((completed / total) * 100);
    atsRunProgress.value = percent;
    atsRunProgress.displayValue = `${percent}%`;
    atsRunProgress.setAttribute("aria-valuenow", String(percent));
    atsRunPercent.textContent = `${percent}%`;
    atsRunMessage.textContent = message;
  };

  const resetAtsRun = () => {
    atsTimers.splice(0).forEach(window.clearTimeout);
    atsStarted = false;
    atsComplete = false;
    atsResultsLink.hidden = true;
    atsRunBadge.textContent = "Running";
    document.querySelectorAll("[data-ats-task]").forEach((item) => {
      item.dataset.state = "waiting";
      item.querySelector("[data-ats-task-status]").textContent = "Waiting";
    });
    updateAtsProgress(0, 1, "Preparing the resume readability check...");
  };

  const selectedAtsJobs = () => Array.from(
    document.querySelectorAll("#recommended [data-ats-select-job]"),
  ).filter((checkbox) => checkbox.checked).map(
    (checkbox, index) => ({
      key: `job-${index + 1}`,
      title: checkbox.closest(".job-card").querySelector("h3").textContent.trim(),
    }),
  );

  const syncAtsSelection = () => {
    const selectors = [...document.querySelectorAll("#recommended [data-ats-select-job]")];
    const selected = selectors.filter((checkbox) => checkbox.checked);
    selectors.forEach((checkbox) => {
      checkbox.closest(".job-card").classList.toggle("is-ats-selected", checkbox.checked);
    });
    atsStartButton.disabled = selected.length === 0;
    atsStartButton.textContent = `Check ${selected.length} selected jobs`;
  };

  const renderAtsJobTasks = (jobs) => {
    atsTaskList.querySelectorAll('[data-ats-task-kind="job"]').forEach((item) => item.remove());
    jobs.forEach((job) => {
      const item = document.createElement("li");
      item.dataset.atsTask = job.key;
      item.dataset.atsTaskKind = "job";
      item.dataset.state = "waiting";
      const mark = document.createElement("span");
      mark.className = "run-mark";
      const title = document.createElement("strong");
      title.textContent = job.title;
      const status = document.createElement("small");
      status.dataset.atsTaskStatus = "";
      status.textContent = "Waiting";
      item.append(mark, title, status);
      atsTaskList.append(item);
    });
    atsRunningProgressTitle.textContent = `Checking 1 resume against ${jobs.length} jobs`;
  };

  const startAtsRun = () => {
    const jobs = selectedAtsJobs();
    if (jobs.length === 0) return;
    resetAtsRun();
    renderAtsJobTasks(jobs);
    atsStarted = true;
    setView("ats-running");
    setRailState("ats");
    headerStatus.textContent = "ATS running";
    setAtsTaskState("resume", "active", "Running");

    atsTimers.push(window.setTimeout(() => {
      setAtsTaskState("resume", "complete", "Complete");
      document.querySelectorAll('[data-ats-task-kind="job"]').forEach((item) => {
        setAtsTaskState(item.dataset.atsTask, "active", "Running");
      });
      updateAtsProgress(1, jobs.length + 1, `Checking ${jobs.length} selected jobs in parallel...`);
    }, 750));

    jobs.forEach((job, index) => {
      const completed = index + 2;
      atsTimers.push(window.setTimeout(() => {
        setAtsTaskState(job.key, "complete", "Complete");
        if (completed < jobs.length + 1) {
          updateAtsProgress(completed, jobs.length + 1, `${completed - 1} of ${jobs.length} job checks complete.`);
          return;
        }
        atsComplete = true;
        atsRunBadge.textContent = "Complete";
        atsResultsLink.hidden = false;
        headerStatus.textContent = "ATS ready";
        updateAtsProgress(jobs.length + 1, jobs.length + 1, `All ${jobs.length} job checks complete.`);
      }, 1700 + index * 600));
    });
  };

  const setStatusLine = (line, label, value) => {
    const heading = document.createElement("strong");
    heading.textContent = `${label}:`;
    line.replaceChildren(heading, ` ${value}`);
  };

  const initializeReviewWorkspace = () => {
    const groupNavigation = document.querySelector(".review-group-nav");
    const groupPanels = [...document.querySelectorAll(".review-groups > section[id]")];
    const reviewGroupOrderKey = "job-scan.mock-review-group-order.v1";
    let draggedGroupTab = null;

    const groupTabs = () => [
      ...groupNavigation.querySelectorAll("[data-review-group-tab]"),
    ];
    const selectReviewGroup = (groupId) => {
      groupTabs().forEach((tab) => {
        if (tab.dataset.reviewGroupTab === groupId) {
          tab.setAttribute("aria-current", "page");
        } else {
          tab.removeAttribute("aria-current");
        }
      });
      groupPanels.forEach((panel) => {
        panel.hidden = panel.id !== groupId;
      });
    };
    const saveReviewGroupOrder = () => {
      try {
        window.localStorage.setItem(
          reviewGroupOrderKey,
          JSON.stringify(groupTabs().map((tab) => tab.dataset.reviewGroupTab)),
        );
      } catch (_error) {
        // The mock remains draggable when browser storage is unavailable.
      }
    };
    const restoreReviewGroupOrder = () => {
      try {
        const saved = JSON.parse(window.localStorage.getItem(reviewGroupOrderKey));
        if (!Array.isArray(saved)) return;
        const tabsById = new Map(
          groupTabs().map((tab) => [tab.dataset.reviewGroupTab, tab]),
        );
        saved.forEach((groupId) => {
          const tab = tabsById.get(groupId);
          if (tab) groupNavigation.append(tab);
        });
      } catch (_error) {
        // Invalid saved order falls back to the document order.
      }
    };
    const clearReviewGroupDragState = () => {
      groupTabs().forEach((tab) => {
        tab.classList.remove(
          "is-dragging",
          "is-drag-over-before",
          "is-drag-over-after",
        );
      });
    };

    restoreReviewGroupOrder();
    groupNavigation.addEventListener("click", (event) => {
      const tab = event.target.closest("[data-review-group-tab]");
      if (!tab) return;
      event.preventDefault();
      selectReviewGroup(tab.dataset.reviewGroupTab);
    });
    groupNavigation.addEventListener("dragstart", (event) => {
      draggedGroupTab = event.target.closest("[data-review-group-tab]");
      if (!draggedGroupTab) return;
      draggedGroupTab.classList.add("is-dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", draggedGroupTab.dataset.reviewGroupTab);
    });
    groupNavigation.addEventListener("dragover", (event) => {
      const target = event.target.closest("[data-review-group-tab]");
      if (!draggedGroupTab || !target || target === draggedGroupTab) return;
      event.preventDefault();
      clearReviewGroupDragState();
      draggedGroupTab.classList.add("is-dragging");
      const bounds = target.getBoundingClientRect();
      const before = event.clientY <= bounds.top + bounds.height / 2;
      target.classList.add(before ? "is-drag-over-before" : "is-drag-over-after");
    });
    groupNavigation.addEventListener("drop", (event) => {
      const target = event.target.closest("[data-review-group-tab]");
      if (!draggedGroupTab || !target || target === draggedGroupTab) return;
      event.preventDefault();
      const bounds = target.getBoundingClientRect();
      const before = event.clientY <= bounds.top + bounds.height / 2;
      groupNavigation.insertBefore(
        draggedGroupTab,
        before ? target : target.nextElementSibling,
      );
      saveReviewGroupOrder();
      draggedGroupTab = null;
      clearReviewGroupDragState();
    });
    groupNavigation.addEventListener("dragend", () => {
      draggedGroupTab = null;
      clearReviewGroupDragState();
    });

    const reviewCards = [...document.querySelectorAll(".review-groups [data-sources]")];
    const sourceSelect = document.querySelector("#source-filter");
    const postedWithinSelect = document.querySelector("#review-posted-within-days");
    const companySizeSelect = document.querySelector("#review-company-size");

    reviewCards.forEach((card) => {
      const offset = Number(card.dataset.postedDaysAgo);
      if (!Number.isFinite(offset)) return;
      const postedAt = new Date();
      postedAt.setHours(0, 0, 0, 0);
      postedAt.setDate(postedAt.getDate() - offset);
      const year = postedAt.getFullYear();
      const month = String(postedAt.getMonth() + 1).padStart(2, "0");
      const day = String(postedAt.getDate()).padStart(2, "0");
      card.dataset.postedAt = `${year}-${month}-${day}`;
    });

    const cardSources = (card) => card.dataset.sources.split(",").filter(Boolean);
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
    const applyReviewFilters = (selectedSources) => {
      const selected = new Set(selectedSources);
      reviewCards.forEach((card) => {
        const matchesSource = cardSources(card).some((source) => selected.has(source));
        card.hidden =
          !matchesSource ||
          !postedWithinWindow(card, postedWithinSelect.value) ||
          !matchesCompanySize(card, companySizeSelect.value);
      });
    };

    const sourceLabels = {
      indeed: "Indeed",
      linkedin: "LinkedIn",
      simplify: "Simplify",
    };
    const sources = [...new Set(reviewCards.flatMap(cardSources))].sort();
    sources.forEach((source) => {
      sourceSelect.append(createSelectItem("ui5-mcb-item", source, sourceLabels[source] || source));
    });
    sourceSelect.placeholder = "Choose sources";
    sourceSelect.selectedValues = sources;
    const applyCurrentFilters = () => applyReviewFilters(sourceSelect.selectedValues);
    sourceSelect.addEventListener("selection-change", applyCurrentFilters);
    postedWithinSelect.addEventListener("change", applyCurrentFilters);
    companySizeSelect.addEventListener("change", applyCurrentFilters);
    applyCurrentFilters();

    const previewCard = document.querySelector("[data-job-preview-card]");
    const detailDialog = document.querySelector("#job-detail-dialog");
    if (previewCard && detailDialog) {
      const openDetails = (event) => {
        if (event.target.closest("a, ui5-button, ui5-checkbox, ui5-select, label, form, ui5-panel")) return;
        detailDialog.showModal();
      };
      previewCard.addEventListener("click", openDetails);
      previewCard.addEventListener("keydown", (event) => {
        if (event.target !== previewCard || !["Enter", " "].includes(event.key)) return;
        event.preventDefault();
        detailDialog.showModal();
      });
      detailDialog.querySelector("[data-close-job-detail]").addEventListener("click", () => {
        detailDialog.close();
      });
      detailDialog.addEventListener("click", (event) => {
        if (event.target === detailDialog) detailDialog.close();
      });
    }

    document.querySelectorAll("form[data-job-action]").forEach((jobForm) => {
      jobForm.addEventListener("submit", (event) => {
        event.preventDefault();
        const card = jobForm.closest(".job-card");
        if (jobForm.dataset.jobAction === "restore") {
          setStatusLine(card.querySelector("[data-machine-status]"), "Machine", "eligible");
          setStatusLine(card.querySelector("[data-effective-status]"), "Effective", "eligible");
          const restored = document.createElement("span");
          restored.className = "restored-label";
          restored.textContent = "Restored";
          card.querySelector(".status-rail").append(restored);
          jobForm.remove();
          return;
        }
        const status = jobForm.querySelector("[name='status']").value;
        const jobKey = CSS.escape(jobForm.dataset.jobKey);
        document.querySelectorAll(`[data-job-key="${jobKey}"] [data-user-status]`).forEach(
          (line) => setStatusLine(line, "User status", status),
        );
        document.querySelectorAll(
          `form[data-job-action="status"][data-job-key="${jobKey}"] [name="status"]`,
        ).forEach((select) => { select.value = status; });
        const button = jobForm.querySelector("ui5-button");
        const buttonLabel = button.textContent;
        button.textContent = "Saved";
        window.setTimeout(() => { button.textContent = buttonLabel; }, 700);
      });
    });

    document.querySelectorAll("[data-scan-delete]").forEach((button) => {
      button.addEventListener("click", () => {
        const row = button.closest("[data-scan-history-id]");
        const list = row.parentElement;
        row.remove();
        if (!list.querySelector("[data-scan-history-id]")) {
          const empty = document.createElement("p");
          empty.className = "ai-provider-empty";
          empty.textContent = "No completed searches yet.";
          list.append(empty);
        }
      });
    });
    document.querySelectorAll("[data-scan-download]").forEach((link) => {
      link.addEventListener("click", (event) => {
        event.preventDefault();
        link.textContent = "Resume ready";
      });
    });
  };

  initializeSearchSelects();
  initializeReviewWorkspace();
  renderAiProviders();
  syncAiRuntime();

  aiRuntime.addEventListener("change", syncAiRuntime);

  document.querySelector("#claude-model").addEventListener("change", (event) => {
    const option = aiRuntime.querySelector("ui5-option[value='claude-code']");
    option.textContent = `Claude Code CLI · ${event.currentTarget.value}`;
  });

  document.querySelector("[data-add-ai-provider]").addEventListener("click", () => {
    openAiEditor();
  });

  aiProviderList.addEventListener("click", (event) => {
    const row = event.target.closest("[data-ai-provider]");
    if (!row) return;
    const provider = aiProviders.find((item) => item.id === row.dataset.aiProvider);
    if (!provider) return;
    if (event.target.closest("[data-edit-ai-provider]")) openAiEditor(provider);
  });

  document.querySelector("[data-cancel-ai-provider]").addEventListener("click", closeAiEditor);

  document.querySelector("[data-discover-ai-models]").addEventListener("click", (event) => {
    const hasStoredKey = editingProviderId !== null;
    if (!aiProviderBaseUrl.value.trim() || (!hasStoredKey && !aiProviderApiKey.value.trim())) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Enter a Base URL and API key before fetching models.";
      return;
    }
    if (!isValidProviderBaseUrl()) {
      aiProviderBaseUrl.valueState = "Negative";
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Enter a valid Base URL.";
      return;
    }
    aiProviderBaseUrl.valueState = "None";
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Fetching models...";
    aiEditorFeedback.removeAttribute("data-state");
    aiEditorFeedback.textContent = "Checking the Anthropic-compatible models endpoint.";
    window.setTimeout(() => {
      setProviderModel(aiProviderModel.value || "deepseek-v4-flash", [
        "deepseek-v4-flash",
        "deepseek-chat",
        "deepseek-reasoner",
      ]);
      aiEditorFeedback.textContent = "3 models found. Choose one or type another model name.";
      button.disabled = false;
      button.textContent = "Fetch models";
    }, 450);
  });

  document.querySelector("[data-save-ai-provider]").addEventListener("click", () => {
    const name = aiProviderName.value.trim();
    const baseUrl = aiProviderBaseUrl.value.trim();
    const model = aiProviderModel.value.trim();
    if (!name || !baseUrl || !model || (editingProviderId === null && !aiProviderApiKey.value.trim())) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Complete provider name, Base URL, API key, and model.";
      return;
    }
    if (!isValidProviderBaseUrl()) {
      aiProviderBaseUrl.valueState = "Negative";
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Enter a valid Base URL.";
      return;
    }
    aiProviderBaseUrl.valueState = "None";
    const existing = aiProviders.find((provider) => provider.id === editingProviderId);
    if (existing) {
      Object.assign(existing, { name, baseUrl, model, effort: aiProviderEffort.value });
    } else {
      const baseId = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "provider";
      let id = baseId;
      let suffix = 2;
      while (aiProviders.some((provider) => provider.id === id)) id = `${baseId}-${suffix++}`;
      aiProviders.push({ id, name, baseUrl, model, effort: aiProviderEffort.value });
    }
    renderAiProviders();
    syncAiRuntime();
    closeAiEditor();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const resume = document.querySelector("#resume");
    if (!resume.files?.length) {
      formError.textContent = "Choose a PDF or DOCX resume before starting the scan.";
      formError.hidden = false;
      return;
    }
    formError.hidden = true;
    scheduledTime = scanTime.value;
    renderSchedule();
    startMockRun();
  });

  document.querySelector("#resume").addEventListener("change", () => {
    formError.hidden = true;
  });

  removeSchedule.addEventListener("click", () => {
    scheduledTime = "";
    scanTime.value = "";
    renderSchedule();
  });

  document.querySelector("#back-button").addEventListener("click", () => {
    resetRun();
    setRailState("setup");
    setView("setup");
    headerStatus.textContent = "Ready";
  });

  reviewLink.addEventListener("click", (event) => {
    event.preventDefault();
    setView("review");
    setRailState("review");
    headerStatus.textContent = "Reviewing";
  });

  document.querySelectorAll("#recommended [data-ats-select-job]").forEach((checkbox) => {
    checkbox.addEventListener("change", syncAtsSelection);
  });
  syncAtsSelection();

  atsStartButton.addEventListener("click", (event) => {
    event.preventDefault();
    startAtsRun();
  });

  document.querySelectorAll("[data-back-to-review]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      setView("review");
      setRailState("review");
      headerStatus.textContent = "Reviewing";
    });
  });

  atsResultsLink.addEventListener("click", (event) => {
    event.preventDefault();
    setView("ats");
    setRailState("ats");
    headerStatus.textContent = "ATS check";
  });

  document.querySelectorAll("[data-ats-job]").forEach((button) => {
    button.addEventListener("click", () => {
      const selectedJob = button.dataset.atsJob;
      document.querySelectorAll("[data-ats-job]").forEach((option) => {
        const active = option === button;
        option.classList.toggle("is-active", active);
        option.setAttribute("aria-pressed", String(active));
      });
      document.querySelectorAll("[data-ats-report]").forEach((report) => {
        report.hidden = report.dataset.atsReport !== selectedJob;
      });
    });
  });

  const showAtsHistory = (row) => {
    document.querySelectorAll("[data-ats-history-id]").forEach((candidate) => {
      candidate.classList.toggle("is-selected", candidate === row);
    });
    const label = row.dataset.atsHistoryLabel;
    const readiness = row.dataset.atsHistoryReadiness;
    const scores = row.dataset.atsHistoryScores.split(",");
    atsHistoryContext.textContent = `Viewing ${label.toLowerCase()}`;
    atsResultScope.textContent = `${label.split(" · ")[0]} · ${scores.length} jobs`;
    resumeReadinessScore.textContent = readiness;
    resumeReadinessScore.parentElement.setAttribute(
      "aria-label",
      `Resume readiness score ${readiness} out of 100`,
    );
    document.querySelectorAll("[data-ats-job-score]").forEach((score, index) => {
      score.textContent = `${scores[index]}%`;
    });
    document.querySelectorAll("[data-ats-report-score]").forEach((score, index) => {
      score.textContent = `${scores[index]}%`;
    });
    atsHistory.open = false;
  };

  atsHistory.addEventListener("click", (event) => {
    const row = event.target.closest("[data-ats-history-id]");
    if (!row) return;
    if (event.target.closest("[data-ats-history-view]")) {
      showAtsHistory(row);
      return;
    }
    if (!event.target.closest("[data-ats-history-delete]")) return;
    const wasSelected = row.classList.contains("is-selected");
    row.remove();
    const remaining = atsHistory.querySelector("[data-ats-history-id]");
    if (wasSelected && remaining) showAtsHistory(remaining);
    if (remaining) return;
    const empty = document.createElement("p");
    empty.className = "ai-provider-empty";
    empty.textContent = "No completed ATS checks yet.";
    atsHistory.querySelector(".scan-history-list").append(empty);
    atsHistoryContext.textContent = "No saved ATS check selected.";
    atsResultScope.textContent = "No saved checks";
  });

  document.querySelector("#new-run-button").addEventListener("click", () => {
    resetRun();
    resetAtsRun();
    setRailState("setup");
    setView("setup");
    headerStatus.textContent = "Ready";
  });

  document.querySelectorAll("[data-nav-step]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const requested = link.dataset.navStep;
      if (requested === "setup") {
        resetRun();
        resetAtsRun();
        setRailState("setup");
        setView("setup");
        headerStatus.textContent = "Ready";
      } else if (requested === "run" && !runView.hidden) {
        setView("run");
      } else if (requested === "review" && !reviewLink.hidden) {
        setView("review");
        setRailState("review");
        headerStatus.textContent = "Reviewing";
      } else if (requested === "ats") {
        if (atsComplete) {
          setView("ats");
          setRailState("ats");
          headerStatus.textContent = "ATS check";
        } else if (atsStarted) {
          setView("ats-running");
          setRailState("ats");
          headerStatus.textContent = "ATS running";
        } else {
          startAtsRun();
        }
      }
    });
  });
})();
