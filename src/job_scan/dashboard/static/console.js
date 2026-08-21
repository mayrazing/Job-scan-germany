(() => {
  const form = document.querySelector("#setup-form");
  const submitButton = form.querySelector("button[type='submit']");
  const setupView = document.querySelector("#setup");
  const runView = document.querySelector("#run-view");
  const reviewView = document.querySelector("#review-view");
  const jobTrackerView = document.querySelector("#job-tracker-view");
  const reviewActions = document.querySelector("#review-actions");
  const atsRunningView = document.querySelector("#ats-running");
  const atsView = document.querySelector("#ats-check");
  const atsTaskList = document.querySelector("[data-ats-task-list]");
  const atsResultsLink = document.querySelector("#ats-results-link");
  const atsStartButton = document.querySelector("[data-open-ats]");
  const atsResumeInput = document.querySelector("#ats-resume");
  const reviewOnlyControls = [
    ...document.querySelectorAll("[data-review-only]"),
  ];
  const jobTrackerOnlyControls = [
    ...document.querySelectorAll("[data-job-tracker-only]"),
  ];
  const atsJobSelectors = () => [
    ...document.querySelectorAll("[data-ats-select-job]"),
  ];
  const atsRunBadge = document.querySelector("#ats-run-badge");
  const atsRunProgress = document.querySelector("#ats-run-progress");
  const atsRunProgressBar = document.querySelector("#ats-run-progress-bar");
  const atsRunPercent = document.querySelector("#ats-run-percent");
  const atsRunMessage = document.querySelector("#ats-run-message");
  const atsJobList = document.querySelector(".ats-job-list");
  const atsHistory = document.querySelector("#ats-history");
  const scanHistory = document.querySelector("#scan-history");
  const reviewLink = document.querySelector("#review-link");
  const headerStatus = document.querySelector("#header-status");
  const formError = document.querySelector("#form-error");
  const progressBar = document.querySelector("#progress-bar");
  const progress = progressBar.closest(".progress");
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
  const aiConfigModal = document.querySelector("#ai-config-modal");
  const aiConfigLockNote = document.querySelector("#ai-config-lock-note");
  const aiSelectionFeedback = document.querySelector("#ai-selection-feedback");
  const saveAiSelectionButton = document.querySelector("[data-save-ai-selection]");
  const claudeCodeSettings = document.querySelector("#claude-code-settings");
  const apiModelSettings = document.querySelector("#api-model-settings");
  const apiModelSummary = document.querySelector("#api-model-summary");
  const apiModelEffort = document.querySelector("#api-model-effort");
  const resumeInput = document.querySelector("#resume");
  const resumeSuggestionStatus = document.querySelector("#resume-suggestion-status");
  const analyzeResumeButton = document.querySelector("#analyze-resume");
  const setupDraftKey = "job-scan.setup-draft.v1";
  const targetCompanies = [
    "bosch",
    "telekom",
    "rohde-schwarz",
    "siemens",
    "dhl",
    "thyssenkrupp",
    "dallmeier",
  ];
  const openCliSources = [
    {
      enabledField: "linkedin_enabled",
      enabledSelector: "#linkedin-enabled",
      limitField: "linkedin_limit",
      limitSelector: "#linkedin-limit",
    },
    {
      enabledField: "indeed_de_enabled",
      enabledSelector: "#indeed-de-enabled",
      limitField: "indeed_de_limit",
      limitSelector: "#indeed-de-limit",
    },
    {
      enabledField: "stepstone_de_enabled",
      enabledSelector: "#stepstone-de-enabled",
      limitField: "stepstone_de_limit",
      limitSelector: "#stepstone-de-limit",
    },
    {
      enabledField: "glassdoor_de_enabled",
      enabledSelector: "#glassdoor-de-enabled",
      limitField: "glassdoor_de_limit",
      limitSelector: "#glassdoor-de-limit",
    },
    {
      enabledField: "simplify_de_enabled",
      enabledSelector: "#simplify-de-enabled",
      limitField: "simplify_de_limit",
      limitSelector: "#simplify-de-limit",
    },
  ];
  let scheduledTime = "";
  let reviewNeedsRefresh = false;
  let aiProviders = [];
  let editingProviderId = null;
  let resumeSuggestionRequest = null;
  let atsStartInFlight = false;
  let activeAtsRunId = null;
  let completedAtsRunId = null;
  let atsCurrentRequestVersion = 0;
  let activeRunId = null;
  let runAiRuntime = null;
  let runAiRuntimeName = null;
  let aiConfigurationLocked = false;
  let aiConfigurationPoll = null;

  const placeholders = {
    "german-level": "Search or type a level",
    "search-terms": "Search or add a role",
    locations: "Search German cities",
    "claude-model": "Search or type a model",
    "ai-provider-model": "Fetch or type a model",
  };

  const resumeSuggestionContainers = {
    "search-terms": "#search-term-suggestions",
  };

  const syncResumeSuggestionButtons = (select, items) => {
    const containerSelector = resumeSuggestionContainers[select.id];
    if (!containerSelector) return;
    document.querySelectorAll(`${containerSelector} [data-suggestion-value]`).forEach((button) => {
      button.disabled = items.includes(button.dataset.suggestionValue);
    });
  };

  const initializeSearchSelect = (select) => {
    if (select.tomselect) return select.tomselect;
    const multiple = select.multiple;
    return new TomSelect(select, {
      plugins: multiple ? { remove_button: { title: "Remove" } } : {},
      create: select.dataset.create === "true",
      createOnBlur: true,
      addPrecedence: true,
      persist: false,
      maxItems: multiple ? null : 1,
      hideSelected: true,
      closeAfterSelect: true,
      placeholder: placeholders[select.id] || "Search or type a value",
      render: {
        option_create(data, escape) {
          return `<div class="create"><span>Add custom value</span><strong>${escape(data.input)}</strong></div>`;
        },
      },
      onInitialize() {
        this.wrapper.dataset.field = select.id || select.name;
      },
      onItemAdd() {
        syncResumeSuggestionButtons(select, this.items);
      },
      onItemRemove() {
        syncResumeSuggestionButtons(select, this.items);
      },
    });
  };

  const initializeSearchSelects = (root = document) => {
    root.querySelectorAll("select[data-search-select]").forEach(initializeSearchSelect);
  };

  const initializeTooltips = (root = document) => {
    root.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((element) => {
      bootstrap.Tooltip.getOrCreateInstance(element);
    });
  };

  const clearResumeSuggestions = () => {
    const container = document.querySelector("#search-term-suggestions");
    container.replaceChildren();
    container.hidden = true;
    document.querySelector("#search-term-suggestion-help").hidden = true;
  };

  const renderResumeSuggestions = (containerSelector, selectSelector, values) => {
    const container = document.querySelector(containerSelector);
    const control = document.querySelector(selectSelector).tomselect;
    container.replaceChildren();
    values.forEach((value) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-sm btn-outline-secondary";
      button.textContent = value;
      button.dataset.suggestionValue = value;
      button.disabled = control.items.includes(value);
      button.addEventListener("click", () => {
        if (!control.options[value]) control.addOption({ value, text: value });
        if (!control.items.includes(value)) control.addItem(value);
        saveSetupDraft();
      });
      container.append(button);
    });
    container.hidden = values.length === 0;
    document.querySelector("#search-term-suggestion-help").hidden = values.length === 0;
  };

  const analyzeResume = async () => {
    const resume = resumeInput.files[0];
    if (!resume) {
      clearResumeSuggestions();
      resumeSuggestionStatus.textContent = "";
      analyzeResumeButton.disabled = true;
      return;
    }
    resumeSuggestionRequest?.abort();
    const request = new AbortController();
    resumeSuggestionRequest = request;
    clearResumeSuggestions();
    resumeSuggestionStatus.textContent = "AI is analyzing the resume...";
    analyzeResumeButton.disabled = true;

    const payload = new FormData();
    payload.append("settings", JSON.stringify({
      ai_runtime: aiRuntime.value,
      claude: {
        model: document.querySelector("#claude-model").tomselect.getValue(),
        effort: document.querySelector("#claude-effort").value,
        thinking_enabled: document.querySelector("#claude-thinking-enabled").checked,
        batch_size: Number(document.querySelector("#claude-batch-size").value),
      },
    }));
    payload.append("resume", resume, resume.name);
    try {
      const response = await fetch("/api/resume-suggestions", {
        method: "POST",
        credentials: "same-origin",
        body: payload,
        signal: request.signal,
      });
      if (!response.ok) throw new Error(await responseError(response));
      const suggestions = await response.json();
      if (resumeSuggestionRequest !== request) return;
      renderResumeSuggestions(
        "#search-term-suggestions",
        "#search-terms",
        suggestions.search_terms,
      );
      resumeSuggestionStatus.textContent = "AI suggestions ready. Click to add.";
    } catch (error) {
      if (error.name === "AbortError") return;
      resumeSuggestionStatus.textContent = error.message || "Could not analyze this resume.";
    } finally {
      if (resumeSuggestionRequest === request) {
        resumeSuggestionRequest = null;
        analyzeResumeButton.disabled = !resumeInput.files[0];
      }
    }
  };

  const providerInitials = (name) => name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase() || "AI";

  const createProviderButton = (label, attribute) => {
    const button = document.createElement("button");
    button.className = "btn btn-outline-secondary";
    button.type = "button";
    button.textContent = label;
    button.toggleAttribute(attribute, true);
    button.toggleAttribute("data-ai-config-control", true);
    button.disabled = aiConfigurationLocked;
    return button;
  };

  const renderAiProviders = () => {
    aiProviderList.replaceChildren();
    if (aiProviders.length === 0) {
      const empty = document.createElement("p");
      empty.className = "ai-provider-empty";
      empty.textContent = "No API models configured. Claude Code CLI remains available.";
      aiProviderList.append(empty);
      return;
    }
    aiProviders.forEach((provider) => {
      const row = document.createElement("article");
      row.className = "ai-provider-row";
      row.dataset.aiProvider = provider.id;

      const mark = document.createElement("div");
      mark.className = "ai-provider-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = providerInitials(provider.display_name);

      const copy = document.createElement("div");
      copy.className = "ai-provider-copy";
      const title = document.createElement("div");
      title.className = "ai-provider-title";
      const name = document.createElement("strong");
      name.textContent = provider.display_name;
      title.append(name);
      const summary = document.createElement("p");
      summary.textContent = `${provider.model} · Effort: ${provider.reasoning_effort}`;
      const url = document.createElement("small");
      url.textContent = provider.base_url;
      copy.append(title, summary, url);

      const actions = document.createElement("div");
      actions.className = "ai-provider-actions";
      const deleteButton = createProviderButton("Delete", "data-delete-ai-provider");
      deleteButton.className = "btn btn-outline-danger";
      deleteButton.setAttribute(
        "aria-label",
        `Delete ${provider.display_name} AI configuration`,
      );
      actions.append(
        createProviderButton("Edit", "data-edit-ai-provider"),
        deleteButton,
      );
      row.append(mark, copy, actions);
      aiProviderList.append(row);
    });
  };

  const selectedAiProvider = (runtime = aiRuntime.value) => {
    if (!runtime.startsWith("api:")) return null;
    const providerId = runtime.slice("api:".length);
    return aiProviders.find((provider) => provider.id === providerId) || null;
  };

  const aiRuntimeName = (runtime) => {
    const selected = selectedAiProvider(runtime);
    const runtimeOption = [...aiRuntime.options].find((option) => option.value === runtime);
    const optionName = runtimeOption?.textContent?.split(" · ")[0];
    return selected
      ? `${selected.display_name} API`
      : runtime === "claude-code" ? "Claude Code" : optionName || "AI";
  };

  const syncRunReviewLabel = () => {
    const runtime = runAiRuntime || aiRuntime.value;
    const runtimeName = runAiRuntimeName || aiRuntimeName(runtime);
    document.querySelector("[data-run-item='review'] strong").textContent = `${runtimeName} review`;
  };

  const syncAiRuntime = () => {
    const previousValue = aiRuntime.value;
    aiRuntime.querySelectorAll("option[value^='api:']").forEach((option) => option.remove());
    aiProviders.forEach((provider) => {
      const option = document.createElement("option");
      option.value = `api:${provider.id}`;
      option.textContent = `${provider.display_name} API · ${provider.model}`;
      aiRuntime.append(option);
    });
    aiRuntime.value = [...aiRuntime.options].some(
      (option) => option.value === previousValue,
    ) ? previousValue : "claude-code";
    const selected = selectedAiProvider();
    const useApi = selected !== null;
    claudeCodeSettings.hidden = Boolean(useApi);
    apiModelSettings.hidden = !useApi;
    if (selected) {
      apiModelSummary.textContent = `${selected.display_name} · ${selected.model}`;
      apiModelSettings.querySelector(".ai-provider-mark").textContent = providerInitials(selected.display_name);
      apiModelEffort.textContent = `Effort: ${selected.reasoning_effort} · Anthropic-compatible API`;
    }
    syncRunReviewLabel();
  };

  const setProviderModels = (selected, options = []) => {
    const control = aiProviderModel.tomselect;
    control.clearOptions();
    [...new Set([selected, ...options].filter(Boolean))].forEach((model) => {
      control.addOption({ value: model, text: model });
    });
    control.refreshOptions(false);
    control.setValue(selected, true);
  };

  const openAiEditor = (provider = null) => {
    editingProviderId = provider?.id || null;
    aiEditorMode.textContent = provider ? "Edit API model" : "New API model";
    aiEditorTitle.textContent = provider?.display_name || "Connect a provider";
    aiProviderName.value = provider?.display_name || "";
    aiProviderBaseUrl.value = provider?.base_url || "";
    aiProviderApiKey.value = "";
    aiProviderApiKey.placeholder = provider
      ? "Configured · enter to replace"
      : "Required for a new configuration";
    setProviderModels(provider?.model || "");
    aiProviderEffort.value = provider?.reasoning_effort || "low";
    aiEditorFeedback.textContent = "";
    aiEditorFeedback.removeAttribute("data-state");
    aiProviderEditor.hidden = false;
    aiProviderName.focus();
  };

  const closeAiEditor = () => {
    aiProviderEditor.hidden = true;
    aiEditorFeedback.textContent = "";
  };

  const loadAiProviders = async () => {
    const response = await fetch("/api/ai/providers", { credentials: "same-origin" });
    if (!response.ok) throw new Error(await responseError(response));
    aiProviders = await response.json();
    renderAiProviders();
    syncAiRuntime();
  };

  const setAiConfigurationControlsDisabled = (disabled) => {
    aiConfigModal.querySelectorAll("[data-ai-config-control]").forEach((control) => {
      control.disabled = disabled;
      if (control.tomselect) {
        if (disabled) control.tomselect.disable();
        else control.tomselect.enable();
      }
    });
  };

  const setAiConfigurationLocked = (locked) => {
    aiConfigurationLocked = Boolean(locked);
    aiConfigLockNote.hidden = !aiConfigurationLocked;
    setAiConfigurationControlsDisabled(aiConfigurationLocked);
  };

  const applyAiConfiguration = (state) => {
    const claudeModel = document.querySelector("#claude-model").tomselect;
    const model = state.claude.model;
    if (!claudeModel.options[model]) {
      claudeModel.addOption({ value: model, text: model });
    }
    claudeModel.setValue(model, true);
    document.querySelector("#claude-effort").value = state.claude.effort;
    document.querySelector("#claude-thinking-enabled").checked =
      state.claude.thinking_enabled;
    aiRuntime.value = state.ai_runtime;
    syncAiRuntime();
    setAiConfigurationLocked(state.locked);
  };

  const loadAiConfiguration = async ({ applySelection = true } = {}) => {
    const response = await fetch("/api/ai/config", { credentials: "same-origin" });
    if (!response.ok) throw new Error(await responseError(response));
    const state = await response.json();
    if (applySelection) applyAiConfiguration(state);
    else setAiConfigurationLocked(state.locked);
    return state;
  };

  const saveAiSelection = async () => {
    if (aiConfigurationLocked) return;
    saveAiSelectionButton.disabled = true;
    aiSelectionFeedback.textContent = "Saving...";
    try {
      const response = await fetch("/api/ai/config", {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ai_runtime: aiRuntime.value,
          claude: {
            model: document.querySelector("#claude-model").tomselect.getValue(),
            effort: document.querySelector("#claude-effort").value,
            thinking_enabled: document.querySelector("#claude-thinking-enabled").checked,
          },
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      applyAiConfiguration(await response.json());
      aiSelectionFeedback.textContent = "AI selection saved.";
    } catch (error) {
      aiSelectionFeedback.textContent = error.message || "Could not save AI selection.";
      await loadAiConfiguration().catch(() => {});
    } finally {
      saveAiSelectionButton.disabled = aiConfigurationLocked;
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
    jobTrackerView.hidden = selected !== "job-tracker";
    reviewActions.hidden = !["review", "job-tracker"].includes(selected);
    reviewOnlyControls.forEach((control) => {
      control.hidden = selected !== "review";
    });
    jobTrackerOnlyControls.forEach((control) => {
      control.hidden = selected !== "job-tracker";
    });
    atsRunningView.hidden = selected !== "ats-run";
    atsView.hidden = selected !== "ats";
    document.querySelectorAll("[data-nav-step]").forEach((link) => {
      const active = link.dataset.navStep === selected;
      link.classList.toggle("btn-primary", active);
      link.classList.toggle("btn-outline-secondary", !active);
      if (active) link.setAttribute("aria-current", "step");
      else link.removeAttribute("aria-current");
    });
  };

  const syncScanSubmit = () => {
    const running = activeRunId !== null;
    submitButton.disabled = running;
    submitButton.textContent = running ? "Scan already running" : "Save and run scan";
  };

  const resetRun = () => {
    runAiRuntime = null;
    runAiRuntimeName = null;
    syncRunReviewLabel();
    syncScanSubmit();
    progressBar.classList.remove("progress-bar-striped", "progress-bar-animated", "bg-danger");
    progressBar.style.width = "0%";
    progress.setAttribute("aria-valuenow", "0");
    progress.removeAttribute("aria-valuetext");
    runPercent.textContent = "0%";
    runMessage.textContent = "Preparing your factual profile...";
    runSummary.hidden = true;
    reviewLink.hidden = true;
    document.querySelectorAll("[data-run-item]").forEach((item) => {
      item.dataset.state = "waiting";
      item.querySelector("small").textContent = "Waiting";
    });
  };

  const showIdleRun = () => {
    resetRun();
    runPercent.textContent = "Idle";
    runMessage.textContent = "No scan is running. Start one from Setup.";
    progress.setAttribute("aria-valuetext", "No scan is running");
  };

  const startRun = () => {
    const submittedAiRuntime = aiRuntime.value;
    const submittedAiRuntimeName = aiRuntimeName(submittedAiRuntime);
    resetRun();
    runAiRuntime = submittedAiRuntime;
    runAiRuntimeName = submittedAiRuntimeName;
    syncRunReviewLabel();
    submitButton.disabled = true;
    setView("run");
    headerStatus.textContent = "Running";
    runPercent.textContent = "10%";
    runMessage.textContent = "Building candidate profile...";
    progressBar.classList.add("progress-bar-striped", "progress-bar-animated");
    progressBar.style.width = "10%";
    progress.setAttribute("aria-valuenow", "10");
    progress.removeAttribute("aria-valuetext");
    const profileItem = document.querySelector('[data-run-item="profile"]');
    profileItem.dataset.state = "active";
    profileItem.querySelector("small").textContent = "Running";
  };

  const runStages = ["profile", "sources", "review", "company_size", "publish"];
  const runStageProgress = { profile: 10, sources: 35, review: 75, company_size: 95, publish: 99 };
  const completedStageLabels = {
    profile: "Profile ready",
    sources: "Sources complete",
    review: "AI review complete",
    company_size: "Company sizes checked",
    publish: "Published",
  };

  const updateRun = (state) => {
    if (state.ai_runtime) {
      runAiRuntime = state.ai_runtime;
      runAiRuntimeName ||= aiRuntimeName(state.ai_runtime);
      syncRunReviewLabel();
    }
    const activeIndex = runStages.indexOf(state.stage);
    const percent = Number.isFinite(state.progress_percent)
      ? state.progress_percent
      : runStageProgress[state.stage];
    runPercent.textContent = `${percent}%`;
    runMessage.textContent = state.message;
    progressBar.style.width = `${percent}%`;
    progress.setAttribute("aria-valuenow", String(percent));
    progress.removeAttribute("aria-valuetext");
    document.querySelectorAll("[data-run-item]").forEach((item) => {
      const itemIndex = runStages.indexOf(item.dataset.runItem);
      if (itemIndex < activeIndex) {
        item.dataset.state = "complete";
        item.querySelector("small").textContent = completedStageLabels[item.dataset.runItem];
      } else if (itemIndex === activeIndex) {
        item.dataset.state = "active";
        const sources = state.source_progress;
        const review = state.review_progress;
        const companySize = state.company_size_progress;
        if (state.stage === "sources" && sources) {
          const jobLabel = sources.found_jobs === 1 ? "job" : "jobs";
          const warningLabel = sources.warning_count === 1 ? "warning" : "warnings";
          const warnings = sources.warning_count > 0
            ? ` · ${sources.warning_count} ${warningLabel}`
            : "";
          item.querySelector("small").textContent = `${sources.completed_sources}/${sources.total_sources} sources · ${sources.found_jobs} ${jobLabel} found${warnings}`;
        } else if (state.stage === "review" && review?.total_batches > 0) {
          item.querySelector("small").textContent = `${review.completed_batches}/${review.total_batches} batches · ${review.completed_jobs}/${review.total_jobs} jobs`;
        } else if (state.stage === "company_size" && companySize?.total_companies > 0) {
          item.querySelector("small").textContent = `${companySize.completed_companies}/${companySize.total_companies} companies`;
        } else {
          item.querySelector("small").textContent = "Running";
        }
      } else {
        item.dataset.state = "waiting";
        item.querySelector("small").textContent = "Waiting";
      }
    });
  };

  const completeRun = (result) => {
    activeRunId = null;
    syncScanSubmit();
    const summary = result.summary;
    const labels = {
      profile: "Profile ready",
      sources: `${summary.occurrence_count} jobs found`,
      review: `${summary.reviewed_count} jobs reviewed`,
      company_size: "Company sizes checked",
      publish: `${summary.eligible_count} eligible jobs`,
    };
    document.querySelectorAll("[data-run-item]").forEach((item) => {
      item.dataset.state = "complete";
      item.querySelector("small").textContent = labels[item.dataset.runItem];
    });
    document.querySelector("#found-count").textContent = summary.occurrence_count;
    document.querySelector("#reviewed-count").textContent = summary.reviewed_count;
    document.querySelector("#eligible-count").textContent = summary.eligible_count;
    document.querySelector("#warning-count").textContent = summary.source_error_count;
    progressBar.classList.remove("progress-bar-striped", "progress-bar-animated");
    progressBar.style.width = "100%";
    progress.setAttribute("aria-valuenow", "100");
    progress.removeAttribute("aria-valuetext");
    runPercent.textContent = "100%";
    runMessage.textContent = "Review queue published with real scan results.";
    runSummary.hidden = false;
    reviewLink.hidden = false;
    scheduledTime = result.schedule.local_time || "";
    scanTime.value = scheduledTime;
    renderSchedule();
    headerStatus.textContent = "Review ready";
    reviewNeedsRefresh = true;
  };

  const failRun = (message) => {
    progressBar.classList.remove("progress-bar-striped", "progress-bar-animated");
    progressBar.classList.add("bg-danger");
    progressBar.style.width = "100%";
    progress.setAttribute("aria-valuenow", "100");
    progress.removeAttribute("aria-valuetext");
    runPercent.textContent = "Failed";
    runMessage.textContent = message;
    headerStatus.textContent = "Failed";
  };

  const selectedItems = (selector) => document.querySelector(selector).tomselect.items;

  const syncOpenCliSource = (source) => {
    const enabled = document.querySelector(source.enabledSelector).checked;
    document.querySelector(source.limitSelector).disabled = !enabled;
  };

  const syncOpenCliSources = () => openCliSources.forEach(syncOpenCliSource);

  const setupDraftFromForm = () => ({
    ai_runtime: aiRuntime.value,
    search_terms: selectedItems("#search-terms"),
    locations: selectedItems("#locations"),
    posted_within_days: document.querySelector("#posted-within-days").value,
    arbeitsagentur_enabled: document.querySelector("#arbeitsagentur-enabled").checked,
    target_companies: targetCompanies.filter(
      (company) => document.querySelector(`#target-company-${company}`).checked,
    ),
    linkedin_enabled: document.querySelector("#linkedin-enabled").checked,
    linkedin_limit: document.querySelector("#linkedin-limit").value,
    indeed_de_enabled: document.querySelector("#indeed-de-enabled").checked,
    indeed_de_limit: document.querySelector("#indeed-de-limit").value,
    stepstone_de_enabled: document.querySelector("#stepstone-de-enabled").checked,
    stepstone_de_limit: document.querySelector("#stepstone-de-limit").value,
    glassdoor_de_enabled: document.querySelector("#glassdoor-de-enabled").checked,
    glassdoor_de_limit: document.querySelector("#glassdoor-de-limit").value,
    simplify_de_enabled: document.querySelector("#simplify-de-enabled").checked,
    simplify_de_limit: document.querySelector("#simplify-de-limit").value,
    minimum_company_size: document.querySelector("#minimum-company-size").value,
    german_level: document.querySelector("#german-level").tomselect.getValue(),
    claude: {
      model: document.querySelector("#claude-model").tomselect.getValue(),
      effort: document.querySelector("#claude-effort").value,
      thinking_enabled: document.querySelector("#claude-thinking-enabled").checked,
      batch_size: document.querySelector("#claude-batch-size").value,
    },
    scheduler: { local_time: scanTime.value },
  });

  const saveSetupDraft = () => {
    try {
      const draft = setupDraftFromForm();
      delete draft.ai_runtime;
      draft.claude = { batch_size: draft.claude.batch_size };
      window.localStorage.setItem(setupDraftKey, JSON.stringify(draft));
    } catch (_error) {
      // The form remains usable when browser storage is disabled or full.
    }
  };

  const restoreSearchSelect = (selector, value) => {
    const control = document.querySelector(selector).tomselect;
    const values = (Array.isArray(value) ? value : [value])
      .filter((item) => typeof item === "string" && item);
    values.forEach((item) => {
      if (!control.options[item]) control.addOption({ value: item, text: item });
    });
    control.setValue(Array.isArray(value) ? values : values[0] || "", true);
  };

  const restoreSetupDraft = () => {
    let draft;
    try {
      const serialized = window.localStorage.getItem(setupDraftKey);
      if (!serialized) return false;
      draft = JSON.parse(serialized);
    } catch (_error) {
      return false;
    }
    if (!draft || typeof draft !== "object" || Array.isArray(draft)) return false;

    restoreSearchSelect("#search-terms", draft.search_terms);
    restoreSearchSelect("#locations", draft.locations);
    restoreSearchSelect("#german-level", draft.german_level);

    const values = {
      "#posted-within-days": draft.posted_within_days,
      "#linkedin-limit": draft.linkedin_limit,
      "#indeed-de-limit": draft.indeed_de_limit,
      "#stepstone-de-limit": draft.stepstone_de_limit,
      "#glassdoor-de-limit": draft.glassdoor_de_limit,
      "#simplify-de-limit": draft.simplify_de_limit,
      "#minimum-company-size": draft.minimum_company_size,
      "#claude-batch-size": draft.claude?.batch_size,
      "#scan-time": draft.scheduler?.local_time,
    };
    Object.entries(values).forEach(([selector, value]) => {
      if (typeof value === "string" || typeof value === "number") {
        document.querySelector(selector).value = String(value);
      }
    });
    if (typeof draft.arbeitsagentur_enabled === "boolean") {
      document.querySelector("#arbeitsagentur-enabled").checked =
        draft.arbeitsagentur_enabled;
    }
    openCliSources.forEach((source) => {
      const enabled = document.querySelector(source.enabledSelector);
      const limit = document.querySelector(source.limitSelector);
      if (typeof draft[source.enabledField] === "boolean") {
        enabled.checked = draft[source.enabledField];
      } else if (
        typeof draft[source.limitField] === "string"
        || typeof draft[source.limitField] === "number"
      ) {
        enabled.checked = Number(draft[source.limitField]) > 0;
      }
      if (Number(draft[source.limitField]) === 0) limit.value = "10";
    });
    if (Array.isArray(draft.target_companies)) {
      targetCompanies.forEach((company) => {
        document.querySelector(`#target-company-${company}`).checked =
          draft.target_companies.includes(company);
      });
    }
    return true;
  };

  const settingsFromForm = () => {
    const draft = setupDraftFromForm();
    return {
      ai_runtime: draft.ai_runtime,
      search_terms: draft.search_terms,
      locations: draft.locations,
      posted_within_days:
        draft.posted_within_days === "" ? null : Number(draft.posted_within_days),
      arbeitsagentur_enabled: draft.arbeitsagentur_enabled,
      target_companies: draft.target_companies,
      linkedin_enabled: draft.linkedin_enabled,
      linkedin_limit: Number(draft.linkedin_limit),
      indeed_de_enabled: draft.indeed_de_enabled,
      indeed_de_limit: Number(draft.indeed_de_limit),
      stepstone_de_enabled: draft.stepstone_de_enabled,
      stepstone_de_limit: Number(draft.stepstone_de_limit),
      glassdoor_de_enabled: draft.glassdoor_de_enabled,
      glassdoor_de_limit: Number(draft.glassdoor_de_limit),
      simplify_de_enabled: draft.simplify_de_enabled,
      simplify_de_limit: Number(draft.simplify_de_limit),
      minimum_company_size: Number(draft.minimum_company_size),
      german_level: draft.german_level,
      claude: {
        model: draft.claude.model,
        effort: draft.claude.effort,
        thinking_enabled: draft.claude.thinking_enabled,
        batch_size: Number(draft.claude.batch_size),
      },
      scheduler: { local_time: draft.scheduler.local_time || null },
    };
  };

  const responseError = async (response) => {
    try {
      const body = await response.json();
      return body.detail || `Request failed (${response.status}).`;
    } catch (_error) {
      return `Request failed (${response.status}).`;
    }
  };

  const wait = (milliseconds) => new Promise((resolve) => {
    window.setTimeout(resolve, milliseconds);
  });

  const atsVisualState = {
    waiting: "waiting",
    running: "active",
    complete: "complete",
    failed: "error",
    skipped: "skipped",
  };

  const renderAtsTasks = (state) => {
    atsTaskList.replaceChildren();
    state.tasks.forEach((task) => {
      const item = document.createElement("li");
      item.dataset.atsTask = task.task_id;
      item.dataset.atsTaskKind = task.kind;
      item.dataset.state = atsVisualState[task.status] || "waiting";
      const mark = document.createElement("span");
      mark.className = "run-mark";
      const label = document.createElement("strong");
      label.textContent = task.label;
      const status = document.createElement("small");
      status.dataset.atsTaskStatus = "";
      status.textContent = task.message;
      item.append(mark, label, status);
      atsTaskList.append(item);
    });
  };

  const selectedAtsJobKeys = () => [...new Set(
    atsJobSelectors()
      .filter((control) => control.checked)
      .map((control) => control.value),
  )];

  const syncAtsSelection = () => {
    const count = selectedAtsJobKeys().length;
    atsStartButton.textContent = `Check ${count} selected jobs`;
    atsStartButton.removeAttribute("aria-disabled");
    const hasResume = Boolean(
      atsResumeInput?.files[0] || atsStartButton.dataset.searchRunId,
    );
    atsStartButton.disabled =
      atsStartInFlight || count === 0 || !hasResume;
    atsJobSelectors().forEach((control) => {
      control.closest(".job-card").classList.toggle("is-ats-selected", control.checked);
    });
  };

  const renderAtsState = (state) => {
    const percent = Number.isFinite(state.progress_percent)
      ? state.progress_percent
      : 0;
    renderAtsTasks(state);
    atsRunProgressBar.style.width = `${percent}%`;
    atsRunProgress.setAttribute("aria-valuenow", String(percent));
    atsRunProgress.removeAttribute("aria-valuetext");
    atsRunPercent.textContent = `${percent}%`;
    atsRunMessage.textContent =
      state.status === "failed" && state.error ? state.error : state.message;
    atsRunBadge.textContent = {
      running: "Running",
      complete: "Complete",
      failed: "Failed",
    }[state.status];
    atsRunProgressBar.classList.toggle("progress-bar-striped", state.status === "running");
    atsRunProgressBar.classList.toggle("progress-bar-animated", state.status === "running");
    atsRunProgressBar.classList.toggle("bg-danger", state.status === "failed");
    atsResultsLink.hidden = state.status !== "complete";
    if (state.status === "complete") {
      activeAtsRunId = null;
      completedAtsRunId = state.run_id;
      atsResultsLink.href = `/setup?ats_run_id=${encodeURIComponent(state.run_id)}#ats-check`;
      headerStatus.textContent = "ATS ready";
    } else if (state.status === "failed") {
      activeAtsRunId = null;
      headerStatus.textContent = "Failed";
    } else {
      headerStatus.textContent = "ATS running";
    }
  };

  const showIdleAts = () => {
    activeAtsRunId = null;
    atsTaskList.replaceChildren();
    atsRunBadge.textContent = "Idle";
    atsRunPercent.textContent = "Idle";
    atsRunMessage.textContent = "No ATS check is running. Start one from Job Tracker.";
    atsRunProgressBar.classList.remove(
      "progress-bar-striped",
      "progress-bar-animated",
      "bg-danger",
    );
    atsRunProgressBar.style.width = "0%";
    atsRunProgress.setAttribute("aria-valuenow", "0");
    atsRunProgress.setAttribute("aria-valuetext", "No ATS check is running");
    atsResultsLink.hidden = true;
  };

  const failAts = (message) => {
    activeAtsRunId = null;
    atsRunBadge.textContent = "Failed";
    atsRunMessage.textContent = message;
    atsRunProgressBar.classList.remove("progress-bar-striped", "progress-bar-animated");
    atsRunProgressBar.classList.add("bg-danger");
    atsResultsLink.hidden = true;
    headerStatus.textContent = "Failed";
  };

  const pollAts = async (runId, initialState) => {
    let state = initialState;
    while (state.status === "running") {
      await wait(500);
      let response;
      try {
        response = await fetch(`/api/ats-runs/${encodeURIComponent(runId)}`, {
          credentials: "same-origin",
          signal: AbortSignal.timeout(10_000),
        });
      } catch (_error) {
        failAts("Connection to ATS service lost. Return to Job Tracker and try again.");
        return;
      }
      if (response.status === 404) {
        failAts("ATS state was lost after the service restarted. Return to Job Tracker and try again.");
        return;
      }
      if (!response.ok) {
        failAts(await responseError(response));
        return;
      }
      state = await response.json();
      renderAtsState(state);
    }
  };

  const loadCurrentAts = async () => {
    const requestVersion = ++atsCurrentRequestVersion;
    try {
      const response = await fetch("/api/ats-runs/current", {
        credentials: "same-origin",
        signal: AbortSignal.timeout(10_000),
      });
      if (requestVersion !== atsCurrentRequestVersion) return;
      if (response.status === 204) {
        if (activeAtsRunId === null && window.location.hash === "#ats-run") {
          showIdleAts();
        }
        return;
      }
      if (!response.ok) return;
      const state = await response.json();
      if (requestVersion !== atsCurrentRequestVersion) return;
      activeAtsRunId = state.run_id;
      renderAtsState(state);
      await pollAts(state.run_id, state);
    } catch (_error) {
      // ATS Run remains usable in its idle state when current-run lookup is unavailable.
    }
  };

  const startAts = async (button) => {
    if (atsStartInFlight) return;
    const searchRunId = button.dataset.searchRunId;
    const jobKeys = selectedAtsJobKeys();
    const uploadedResume = atsResumeInput?.files[0];
    const selectedResumeId = document.body.dataset.selectedResumeId;
    if (
      (!searchRunId && !uploadedResume && !selectedResumeId)
      || jobKeys.length === 0
    ) {
      return;
    }
    atsCurrentRequestVersion += 1;
    atsStartInFlight = true;
    syncAtsSelection();
    try {
      const payload = new FormData();
      payload.append("job_keys", JSON.stringify(jobKeys));
      if (searchRunId) payload.append("search_run_id", searchRunId);
      if (uploadedResume) payload.append("resume", uploadedResume, uploadedResume.name);
      else if (selectedResumeId) payload.append("resume_id", selectedResumeId);
      const response = await fetch("/api/ats-runs", {
        method: "POST",
        credentials: "same-origin",
        body: payload,
      });
      if (!response.ok) throw new Error(await responseError(response));
      const state = await response.json();
      activeAtsRunId = state.run_id;
      setView("ats-run");
      window.history.replaceState(null, "", "#ats-run");
      renderAtsState(state);
      await pollAts(state.run_id, state);
    } catch (error) {
      window.alert(error.message || "Could not start ATS check.");
    } finally {
      atsStartInFlight = false;
      syncAtsSelection();
    }
  };

  const pollRun = async (runId) => {
    while (true) {
      await wait(500);
      let response;
      try {
        response = await fetch(`/api/setup-and-scan/${encodeURIComponent(runId)}`, {
          credentials: "same-origin",
          signal: AbortSignal.timeout(10_000),
        });
      } catch (_error) {
        failRun("Connection to scan service lost. Restart the service and try again.");
        return;
      }
      if (response.status === 404) {
        activeRunId = null;
        syncScanSubmit();
        failRun("Scan state was lost after the service restarted. Start the scan again.");
        return;
      }
      if (!response.ok) {
        failRun(await responseError(response));
        return;
      }
      const state = await response.json();
      if (state.status === "complete") {
        completeRun(state.result);
        return;
      }
      if (state.status === "failed") {
        activeRunId = null;
        syncScanSubmit();
        failRun(state.error || "Setup or scan failed.");
        return;
      }
      updateRun(state);
    }
  };

  const loadCurrentRun = async () => {
    try {
      const response = await fetch("/api/setup-and-scan/current", {
        credentials: "same-origin",
        signal: AbortSignal.timeout(10_000),
      });
      if (response.status === 204) {
        activeRunId = null;
        syncScanSubmit();
        return;
      }
      if (!response.ok) return;
      const state = await response.json();
      activeRunId = state.run_id;
      syncScanSubmit();
      updateRun(state);
      headerStatus.textContent = "Running";
      await pollRun(state.run_id);
    } catch (_error) {
      // Setup remains usable when the optional current-run lookup is unavailable.
    }
  };

  const loadSchedule = async (preserveDraft) => {
    try {
      const response = await fetch("/api/schedule", { credentials: "same-origin" });
      if (!response.ok) return;
      const state = await response.json();
      scheduledTime = state.installed ? state.local_time || "" : "";
      if (!preserveDraft) scanTime.value = scheduledTime;
      renderSchedule();
    } catch (_error) {
      // The setup and manual scan flow remains available when scheduler status is unavailable.
    }
  };

  initializeSearchSelects();
  initializeTooltips();
  [reviewView, jobTrackerView].forEach((view) => {
    view.addEventListener("change", (event) => {
      if (event.target.matches("[data-ats-select-job]")) syncAtsSelection();
    });
  });
  document.addEventListener("job-scan:review-updated", () => {
    initializeTooltips(reviewView);
    initializeTooltips(jobTrackerView);
    syncAtsSelection();
  });
  atsResumeInput?.addEventListener("change", syncAtsSelection);
  const syncAtsDefaultResumeLabel = () => {
    const defaultLabel = document.querySelector("[data-ats-default-resume]");
    const select = document.querySelector("[data-global-resume-select]");
    if (!defaultLabel || !select) return;
    const uploaded = atsResumeInput?.files?.[0];
    if (uploaded) {
      defaultLabel.textContent = `Default: ${uploaded.name}`;
    } else {
      const option = select.selectedOptions[0];
      const filename = option?.dataset.resumeFilename;
      const createdAt = option?.dataset.resumeCreatedAt;
      defaultLabel.textContent = filename
        ? `Default: ${filename}${createdAt ? ` (${createdAt})` : ""}`
        : "Upload a PDF or DOCX resume";
    }
  };
  atsResumeInput?.addEventListener("change", syncAtsDefaultResumeLabel);
  atsStartButton.addEventListener("click", () => startAts(atsStartButton));
  syncAtsSelection();
  const restoredSetupDraft = restoreSetupDraft();
  syncOpenCliSources();
  if (restoredSetupDraft) saveSetupDraft();
  renderSchedule();
  loadSchedule(restoredSetupDraft);
  loadAiProviders().then(loadAiConfiguration).catch((error) => {
    formError.textContent = error.message || "Could not load AI configurations.";
    formError.hidden = false;
  });

  form.addEventListener("input", saveSetupDraft);
  form.addEventListener("change", saveSetupDraft);
  openCliSources.forEach((source) => {
    document.querySelector(source.enabledSelector).addEventListener("change", () => {
      syncOpenCliSource(source);
    });
  });
  aiRuntime.addEventListener("change", syncAiRuntime);
  saveAiSelectionButton.addEventListener("click", saveAiSelection);
  aiConfigModal.addEventListener("show.bs.modal", () => {
    aiSelectionFeedback.textContent = "";
    setAiConfigurationControlsDisabled(true);
    loadAiConfiguration().catch((error) => {
      aiSelectionFeedback.textContent =
        error.message || "Could not load AI configuration.";
      setAiConfigurationControlsDisabled(true);
    });
    window.clearInterval(aiConfigurationPoll);
    aiConfigurationPoll = window.setInterval(() => {
      loadAiConfiguration({ applySelection: false }).catch(() => {});
    }, 1000);
  });
  aiConfigModal.addEventListener("hidden.bs.modal", () => {
    window.clearInterval(aiConfigurationPoll);
    aiConfigurationPoll = null;
  });

  document.querySelector("#claude-model").tomselect.on("change", (model) => {
    const option = aiRuntime.querySelector("option[value='claude-code']");
    option.textContent = `Claude Code CLI · ${model}`;
  });

  document.querySelector("[data-add-ai-provider]").addEventListener("click", () => {
    openAiEditor();
  });

  aiProviderList.addEventListener("click", async (event) => {
    const row = event.target.closest("[data-ai-provider]");
    if (!row) return;
    const provider = aiProviders.find((item) => item.id === row.dataset.aiProvider);
    if (!provider) return;
    if (event.target.closest("[data-edit-ai-provider]")) {
      openAiEditor(provider);
    }
    if (event.target.closest("[data-delete-ai-provider]")) {
      if (!window.confirm(`Delete AI configuration ${provider.display_name}?`)) return;
      try {
        const response = await fetch(
          `/api/ai/providers/${encodeURIComponent(provider.id)}`,
          { method: "DELETE", credentials: "same-origin" },
        );
        if (!response.ok) throw new Error(await responseError(response));
        if (editingProviderId === provider.id) closeAiEditor();
        await loadAiProviders();
        await loadAiConfiguration();
        saveSetupDraft();
      } catch (error) {
        window.alert(error.message || "Could not delete AI configuration.");
        await loadAiConfiguration().catch(() => {});
      }
    }
  });

  document.querySelector("[data-cancel-ai-provider]").addEventListener("click", closeAiEditor);

  document.querySelector("[data-discover-ai-models]").addEventListener("click", async (event) => {
    if (!aiProviderBaseUrl.value.trim() || (!editingProviderId && !aiProviderApiKey.value.trim())) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Enter a Base URL and API key before fetching models.";
      return;
    }
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Fetching models...";
    aiEditorFeedback.removeAttribute("data-state");
    aiEditorFeedback.textContent = "Checking the Anthropic-compatible models endpoint.";
    try {
      const response = await fetch("/api/ai/models/discover", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          provider_id: editingProviderId,
          base_url: aiProviderBaseUrl.value.trim(),
          api_key: aiProviderApiKey.value.trim() || null,
        }),
      });
      if (!response.ok) throw new Error(await responseError(response));
      const models = await response.json();
      const selected = aiProviderModel.tomselect.getValue() || models[0]?.id || "";
      setProviderModels(selected, models.map((model) => model.id));
      aiEditorFeedback.textContent = `${models.length} models found. Choose one or type another model name.`;
    } catch (error) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = error.message || "Could not fetch models.";
    } finally {
      button.disabled = aiConfigurationLocked;
      button.textContent = "Fetch models";
    }
  });

  document.querySelector("[data-save-ai-provider]").addEventListener("click", async () => {
    const payload = {
      display_name: aiProviderName.value.trim(),
      base_url: aiProviderBaseUrl.value.trim(),
      api_key: aiProviderApiKey.value.trim() || null,
      model: aiProviderModel.tomselect.getValue().trim(),
      reasoning_effort: aiProviderEffort.value,
    };
    if (!payload.display_name || !payload.base_url || !payload.model || (!editingProviderId && !payload.api_key)) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = "Complete provider name, Base URL, API key, and model.";
      return;
    }
    try {
      const path = editingProviderId
        ? `/api/ai/providers/${encodeURIComponent(editingProviderId)}`
        : "/api/ai/providers";
      const response = await fetch(path, {
        method: editingProviderId ? "PUT" : "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await responseError(response));
      await loadAiProviders();
      await loadAiConfiguration();
      closeAiEditor();
    } catch (error) {
      aiEditorFeedback.dataset.state = "error";
      aiEditorFeedback.textContent = error.message || "Could not save AI configuration.";
      await loadAiConfiguration().catch(() => {});
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    const resume = form.elements.namedItem("resume");
    if (resume.files.length === 0) {
      formError.textContent = "Choose a PDF or DOCX resume before starting the scan.";
      formError.hidden = false;
      return;
    }
    let settings;
    try {
      settings = settingsFromForm();
    } catch (error) {
      formError.textContent = error.message;
      formError.hidden = false;
      return;
    }

    saveSetupDraft();
    formError.hidden = true;
    startRun();
    const payload = new FormData();
    payload.append("settings", JSON.stringify(settings));
    payload.append("resume", resume.files[0], resume.files[0].name);
    try {
      const response = await fetch("/api/setup-and-scan", {
        method: "POST",
        credentials: "same-origin",
        body: payload,
      });
      if (!response.ok) throw new Error(await responseError(response));
      const state = await response.json();
      activeRunId = state.run_id;
      syncScanSubmit();
      updateRun(state);
      await pollRun(state.run_id);
    } catch (error) {
      const message = error instanceof TypeError
        ? "Connection to scan service lost. Restart the service and try again."
        : error.message || "Setup or scan failed.";
      failRun(message);
    }
  });

  resumeInput.addEventListener("change", () => {
    formError.hidden = true;
    resumeSuggestionRequest?.abort();
    clearResumeSuggestions();
    resumeSuggestionStatus.textContent = "";
    analyzeResumeButton.disabled = !resumeInput.files[0];
  });
  analyzeResumeButton.addEventListener("click", analyzeResume);

  removeSchedule.addEventListener("click", async () => {
    removeSchedule.disabled = true;
    try {
      const response = await fetch("/api/schedule", {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      scheduledTime = "";
      scanTime.value = "";
      renderSchedule();
      saveSetupDraft();
    } catch (error) {
      formError.textContent = error.message || "Could not delete schedule.";
      formError.hidden = false;
      removeSchedule.disabled = false;
    }
  });

  scanHistory?.addEventListener("click", async (event) => {
    const deleteButton = event.target.closest("[data-scan-delete]");
    if (!deleteButton) return;
    const row = deleteButton.closest("[data-scan-history-id]");
    const runId = row.dataset.scanHistoryId;
    const candidate = row.querySelector(".ai-provider-title strong").textContent.trim();
    if (!window.confirm(`Delete the complete search history for ${candidate}?`)) return;
    deleteButton.disabled = true;
    try {
      const response = await fetch(`/api/scan-history/${encodeURIComponent(runId)}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      const result = await response.json();
      if (result.deleted_latest) {
        window.location.assign("/setup#setup");
        return;
      }
      if (document.body.dataset.reviewRunId === runId) {
        window.location.assign("/setup#review");
        return;
      }
      if (result.resume_deleted) {
        window.location.assign("/setup#review");
        return;
      }
      row.remove();
    } catch (error) {
      deleteButton.disabled = false;
      window.alert(error.message || "Could not delete this search history.");
    }
  });

  atsJobList?.addEventListener("click", (event) => {
    const selected = event.target.closest("[data-ats-job]");
    if (!selected) return;
    const jobKey = selected.dataset.atsJob;
    atsJobList.querySelectorAll("[data-ats-job]").forEach((button) => {
      const active = button.dataset.atsJob === jobKey;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    atsView.querySelectorAll("[data-ats-report]").forEach((report) => {
      report.hidden = report.dataset.atsReport !== jobKey;
    });
  });

  atsHistory?.addEventListener("click", async (event) => {
    const deleteButton = event.target.closest("[data-ats-history-delete]");
    if (!deleteButton) return;
    const row = deleteButton.closest("[data-ats-history-id]");
    const runId = row.dataset.atsHistoryId;
    if (!window.confirm("Delete this ATS check and its saved resume?")) return;
    deleteButton.disabled = true;
    try {
      const response = await fetch(`/api/ats-history/${encodeURIComponent(runId)}`, {
        method: "DELETE",
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (document.body.dataset.atsRunId === runId) {
        window.location.assign("/setup#ats-check");
        return;
      }
      row.remove();
    } catch (error) {
      deleteButton.disabled = false;
      window.alert(error.message || "Could not delete this ATS check.");
    }
  });

  document.querySelector("#back-button").addEventListener("click", () => {
    if (activeRunId === null) resetRun();
    setView("setup");
    headerStatus.textContent = activeRunId === null ? "Ready" : "Running";
    window.history.replaceState(null, "", "#setup");
  });

  const openReview = () => {
    if (reviewNeedsRefresh) {
      if (window.location.search) {
        window.location.assign("/setup#review");
      } else {
        window.history.replaceState(null, "", "#review");
        window.location.reload();
      }
      return;
    }
    window.history.replaceState(null, "", "#review");
    setView("review");
    headerStatus.textContent = "Reviewing";
  };

  const openJobTracker = () => {
    if (reviewNeedsRefresh) {
      if (window.location.search) {
        window.location.assign("/setup#job-tracker");
      } else {
        window.history.replaceState(null, "", "#job-tracker");
        window.location.reload();
      }
      return;
    }
    window.history.replaceState(null, "", "#job-tracker");
    setView("job-tracker");
    headerStatus.textContent = "Job tracker";
  };

  const openAtsResults = () => {
    window.history.replaceState(null, "", "#ats-check");
    setView("ats");
    headerStatus.textContent = "ATS results";
  };

  const openAtsRun = () => {
    window.history.replaceState(null, "", "#ats-run");
    setView("ats-run");
    if (activeAtsRunId === null) {
      showIdleAts();
      headerStatus.textContent = "Ready";
    } else {
      headerStatus.textContent = "ATS running";
    }
  };

  document.querySelectorAll("[data-local-datetime]").forEach((time) => {
    const instant = new Date(time.dateTime);
    if (!Number.isNaN(instant.getTime())) {
      time.textContent = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(instant);
    }
  });

  document
    .querySelectorAll("[data-global-resume-select] option[data-resume-created-at-iso]")
    .forEach((option) => {
      const instant = new Date(option.dataset.resumeCreatedAtIso);
      if (Number.isNaN(instant.getTime())) return;
      const local = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(instant);
      option.dataset.resumeCreatedAt = local;
      const filename = option.dataset.resumeFilename;
      if (filename) {
        option.textContent = `${filename} (${local})`;
      }
    });
  syncAtsDefaultResumeLabel();

  reviewLink.addEventListener("click", (event) => {
    event.preventDefault();
    openReview();
  });

  document.querySelectorAll("[data-back-to-job-tracker]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      openJobTracker();
    });
  });

  document.querySelector("#new-run-button").addEventListener("click", () => {
    window.location.assign("/setup#setup");
  });

  document.querySelectorAll("[data-nav-step]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      const requested = link.dataset.navStep;
      if (requested === "setup") {
        if (window.location.search) {
          window.location.assign("/setup#setup");
          return;
        }
        if (activeRunId === null) resetRun();
        setView("setup");
        headerStatus.textContent = activeRunId === null ? "Ready" : "Running";
        window.history.replaceState(null, "", "#setup");
      } else if (requested === "run") {
        if (activeRunId === null) {
          showIdleRun();
          headerStatus.textContent = "Ready";
        } else {
          headerStatus.textContent = "Running";
        }
        setView("run");
        window.history.replaceState(null, "", "#run");
      } else if (requested === "review") {
        openReview();
      } else if (requested === "job-tracker") {
        openJobTracker();
      } else if (requested === "ats-run") {
        openAtsRun();
        if (activeAtsRunId === null) loadCurrentAts();
      } else if (requested === "ats") {
        if (completedAtsRunId) {
          window.location.assign(
            `/setup?ats_run_id=${encodeURIComponent(completedAtsRunId)}#ats-check`,
          );
        } else {
          openAtsResults();
        }
      }
    });
  });

  const reviewHashes = new Set([
    "#review",
    "#recommended",
    "#pending",
    "#excluded",
    "#history",
    "#history-stale",
    "#history-closed",
  ]);
  const jobTrackerHashes = new Set([
    "#job-tracker",
    "#saved",
    "#applied",
    "#interviewing",
    "#offer",
    "#withdrawn",
    "#rejected",
    "#ignored",
  ]);
  const applyHashView = () => {
    if (window.location.hash === "#run") {
      if (activeRunId === null) {
        showIdleRun();
        headerStatus.textContent = "Ready";
      } else {
        headerStatus.textContent = "Running";
      }
      setView("run");
      return;
    }
    if (["#ats-run", "#ats-running"].includes(window.location.hash)) {
      openAtsRun();
      return;
    }
    if (window.location.hash === "#ats-check") {
      openAtsResults();
      return;
    }
    if (reviewHashes.has(window.location.hash)) {
      setView("review");
      headerStatus.textContent = "Reviewing";
      return;
    }
    if (jobTrackerHashes.has(window.location.hash)) {
      setView("job-tracker");
      headerStatus.textContent = "Job tracker";
    }
  };
  window.addEventListener("hashchange", applyHashView);
  applyHashView();
  loadCurrentRun();
  loadCurrentAts();
})();
