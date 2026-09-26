import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
import path from "path";

export default defineConfig({
  plugins: [vue()],
  base: "/assets/domino_stream/console/",
  build: {
    outDir: path.resolve(__dirname, "../domino_stream/public/console"),
    emptyOutDir: true,
    manifest: true,
  },
  server: {
    port: 8088,
  },
});
