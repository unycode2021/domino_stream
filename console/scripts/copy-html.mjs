import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "fs";
import { dirname, resolve } from "path";
import { fileURLToPath } from "url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = resolve(__dirname, "..");
const builtHtml = resolve(root, "../domino_stream/public/console/index.html");
const wwwHtml = resolve(root, "../domino_stream/www/domino_stream.html");

mkdirSync(dirname(wwwHtml), { recursive: true });

let html = readFileSync(builtHtml, "utf8");
// Inject Frappe CSRF placeholder replaced by Jinja in www template
if (!html.includes("csrf_token")) {
  html = html.replace(
    "</head>",
    `  <script>window.csrf_token = "{{ csrf_token }}";</script>\n  </head>`
  );
}
writeFileSync(wwwHtml, html);
console.log("Copied console HTML to www/domino_stream.html");
