(() => {
  const adaptedAttribute = "data-ui5-adapted";

  const copyAttributes = (source, target, skipped = []) => {
    [...source.attributes].forEach(({ name, value }) => {
      if (!skipped.includes(name)) target.setAttribute(name, value);
    });
    target.setAttribute(adaptedAttribute, "");
    return target;
  };

  const replaceElement = (source, target) => {
    source.replaceWith(target);
    return target;
  };

  const moveChildren = (source, target) => {
    while (source.firstChild) target.append(source.firstChild);
  };

  const adaptCards = () => {
    document.querySelectorAll("article.console-card, article.job-card, article.ats-report").forEach((card) => {
      const ui5Card = copyAttributes(card, document.createElement("ui5-card"));
      moveChildren(card, ui5Card);
      replaceElement(card, ui5Card);
    });
  };

  const adaptAccordion = () => {
    const accordion = document.querySelector("#advanced-settings");
    if (!accordion) return;
    const panel = copyAttributes(accordion, document.createElement("ui5-panel"), ["class"]);
    panel.className = accordion.className;
    panel.headerText = "Advanced settings";
    panel.collapsed = true;
    const body = accordion.querySelector(".accordion-body");
    if (body) moveChildren(body, panel);
    replaceElement(accordion, panel);
  };

  const adaptDetails = () => {
    document.querySelectorAll("details").forEach((details) => {
      const panel = copyAttributes(details, document.createElement("ui5-panel"), ["open"]);
      const summary = details.querySelector(":scope > summary");
      panel.headerText = summary?.textContent.trim().replace(/\s+/g, " ") || "Details";
      panel.collapsed = !details.open;
      [...details.childNodes].forEach((child) => {
        if (child !== summary) panel.append(child);
      });
      Object.defineProperty(panel, "open", {
        configurable: true,
        get() {
          return !this.collapsed;
        },
        set(value) {
          this.collapsed = !value;
          this.toggleAttribute("open", Boolean(value));
        },
      });
      panel.toggleAttribute("open", details.open);
      replaceElement(details, panel);
    });
  };

  const adaptFileInput = (input) => {
    const uploader = copyAttributes(input, document.createElement("ui5-file-uploader"), ["type", "class"]);
    uploader.className = input.className;
    const button = document.createElement("ui5-button");
    button.textContent = "Choose resume";
    button.setAttribute(adaptedAttribute, "");
    uploader.append(button);
    return replaceElement(input, uploader);
  };

  const adaptCheckbox = (input) => {
    const checkbox = copyAttributes(input, document.createElement("ui5-checkbox"), ["type", "class"]);
    checkbox.className = input.className;
    checkbox.checked = input.checked;
    return replaceElement(input, checkbox);
  };

  const adaptInput = (input) => {
    if (input.type === "file") return adaptFileInput(input);
    if (input.type === "checkbox") return adaptCheckbox(input);

    const tags = {
      number: "ui5-step-input",
      time: "ui5-time-picker",
    };
    const control = copyAttributes(
      input,
      document.createElement(tags[input.type] || "ui5-input"),
      ["class", "type"],
    );
    control.className = input.className;
    control.value = input.value;
    if (control.localName === "ui5-input") {
      const inputTypes = {
        email: "Email",
        password: "Password",
        search: "Search",
        tel: "Tel",
        url: "URL",
      };
      control.type = inputTypes[input.type] || "Text";
    }
    if (control.localName === "ui5-time-picker") control.formatPattern = "HH:mm";
    return replaceElement(input, control);
  };

  const createSelectItem = (tagName, option) => {
    const item = document.createElement(tagName);
    item.textContent = option.textContent;
    item.setAttribute("text", option.textContent);
    item.setAttribute("value", option.value || option.textContent);
    if (option.disabled) item.disabled = true;
    if (option.selected) item.selected = true;
    return item;
  };

  const adaptSelect = (select) => {
    const searchable = select.hasAttribute("data-search-select");
    const tagName = select.multiple
      ? "ui5-multi-combobox"
      : searchable
        ? "ui5-combobox"
        : "ui5-select";
    const itemTagName = select.multiple
      ? "ui5-mcb-item"
      : searchable
        ? "ui5-cb-item"
        : "ui5-option";
    const control = copyAttributes(select, document.createElement(tagName), ["class", "multiple"]);
    control.className = select.className;
    const selectedValues = [...select.options].filter((option) => option.selected).map((option) => option.value);
    [...select.options].forEach((option) => control.append(createSelectItem(itemTagName, option)));
    if (select.multiple) control.selectedValues = selectedValues;
    else control.value = select.value;
    return replaceElement(select, control);
  };

  const adaptButton = (button) => {
    const control = copyAttributes(button, document.createElement("ui5-button"), ["type"]);
    moveChildren(button, control);
    control.type = button.type === "submit" ? "Submit" : button.type === "reset" ? "Reset" : "Button";
    if (button.classList.contains("btn-primary")) control.design = "Emphasized";
    if (button.classList.contains("btn-outline-danger")) control.design = "Negative";
    return replaceElement(button, control);
  };

  const adaptButtonLink = (link) => {
    const button = copyAttributes(link, document.createElement("ui5-button"), ["href", "target", "rel"]);
    moveChildren(link, button);
    button.type = "Button";
    button.dataset.href = link.href;
    button.dataset.target = link.target;
    if (link.classList.contains("btn-primary")) button.design = "Emphasized";
    button.addEventListener("click", (event) => {
      queueMicrotask(() => {
        if (event.defaultPrevented || !button.dataset.href) return;
        if (button.dataset.target === "_blank") window.open(button.dataset.href, "_blank", "noopener,noreferrer");
        else window.location.href = button.dataset.href;
      });
    });
    return replaceElement(link, button);
  };

  const adaptProgress = () => {
    document.querySelectorAll("div.progress").forEach((progress) => {
      const indicator = copyAttributes(progress, document.createElement("ui5-progress-indicator"));
      indicator.value = Number(progress.getAttribute("aria-valuenow") || 0);
      indicator.displayValue = `${indicator.value}%`;
      replaceElement(progress, indicator);
    });
    document.querySelectorAll("progress").forEach((progress) => {
      const indicator = copyAttributes(progress, document.createElement("ui5-progress-indicator"), ["value", "max"]);
      indicator.value = Number(progress.value || 0);
      indicator.displayValue = progress.textContent.trim() || `${indicator.value}%`;
      replaceElement(progress, indicator);
    });
  };

  const adaptDialog = () => {
    document.querySelectorAll("dialog").forEach((dialog) => {
      const ui5Dialog = copyAttributes(dialog, document.createElement("ui5-dialog"));
      moveChildren(dialog, ui5Dialog);
      ui5Dialog.showModal = () => { ui5Dialog.open = true; };
      ui5Dialog.close = () => { ui5Dialog.open = false; };
      replaceElement(dialog, ui5Dialog);
    });
  };

  adaptCards();
  adaptAccordion();
  adaptDetails();
  document.querySelectorAll("input").forEach(adaptInput);
  document.querySelectorAll("select").forEach(adaptSelect);
  document.querySelectorAll("button").forEach(adaptButton);
  document.querySelectorAll("a.btn").forEach(adaptButtonLink);
  adaptProgress();
  adaptDialog();
  document.documentElement.dataset.ui5Ready = "true";
})();
