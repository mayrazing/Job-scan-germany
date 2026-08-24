import { registerLocaleDataLoader } from "@ui5/webcomponents-base/dist/asset-registries/LocaleData.js";
import enLocaleData from "@ui5/webcomponents-localization/dist/generated/assets/cldr/en.json";

import "@ui5/webcomponents/dist/Button.js";
import "@ui5/webcomponents/dist/Card.js";
import "@ui5/webcomponents/dist/CheckBox.js";
import "@ui5/webcomponents/dist/ComboBox.js";
import "@ui5/webcomponents/dist/ComboBoxItem.js";
import "@ui5/webcomponents/dist/Dialog.js";
import "@ui5/webcomponents/dist/FileUploader.js";
import "@ui5/webcomponents/dist/Input.js";
import "@ui5/webcomponents/dist/MultiComboBox.js";
import "@ui5/webcomponents/dist/MultiComboBoxItem.js";
import "@ui5/webcomponents/dist/Option.js";
import "@ui5/webcomponents/dist/Panel.js";
import "@ui5/webcomponents/dist/ProgressIndicator.js";
import "@ui5/webcomponents/dist/Select.js";
import "@ui5/webcomponents/dist/StepInput.js";
import "@ui5/webcomponents/dist/TimePicker.js";

registerLocaleDataLoader("en", async () => enLocaleData);
