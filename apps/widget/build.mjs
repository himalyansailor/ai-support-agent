import { build } from "esbuild";
import { statSync } from "node:fs";

const MAX_BYTES = 15360;
const outfile = "../web/public/widget/v1.js";

await build({
  entryPoints: ["src/loader.js"],
  bundle: true,
  minify: true,
  format: "iife",
  target: "es2018",
  outfile,
});

const size = statSync(outfile).size;
console.log(`widget bundle: ${size} bytes (limit ${MAX_BYTES})`);
if (size > MAX_BYTES) {
  console.error(`widget bundle exceeds ${MAX_BYTES} bytes`);
  process.exit(1);
}
