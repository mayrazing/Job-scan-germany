import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { defineConfig } from "vite";

const prototypeDirectory = dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  define: {
    "import.meta.url": JSON.stringify(""),
  },
  build: {
    target: "es2022",
    outDir: prototypeDirectory,
    emptyOutDir: false,
    lib: {
      entry: resolve(prototypeDirectory, "ui5-entry.js"),
      formats: ["iife"],
      name: "JobScanUi5",
      fileName: () => "ui5-bundle.js",
    },
  },
});
