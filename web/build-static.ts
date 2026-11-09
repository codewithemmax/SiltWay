import { mkdir, readFile, rm, writeFile } from "fs/promises";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

declare const Bun: {
  build: (config: Record<string, unknown>) => Promise<{
    success: boolean;
    outputs: Array<{ path: string; text: () => Promise<string> }>;
    logs: Array<{ message: string }>;
  }>;
};

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, "dist");

await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });

const result = await Bun.build({
  entrypoints: [join(root, "src", "client.tsx")],
  target: "browser",
  outdir: output,
  naming: "bundle.js",
  define: {
    "process.env.NEXT_PUBLIC_API_URL": JSON.stringify(
      process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
    ),
    "process.env.NEXT_PUBLIC_MAPBOX_TOKEN": JSON.stringify(
      process.env.NEXT_PUBLIC_MAPBOX_TOKEN || ""
    )
  }
});

if (!result.success) {
  const logs = result.logs.map((log) => log.message).join("\n");
  throw new Error(`Client bundle build failed:\n${logs}`);
}

const css = await readFile(join(root, "src", "app", "globals.css"), "utf8");
await writeFile(join(output, "globals.css"), css);
await writeFile(
  join(output, "index.html"),
  `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Flood-Resilient Supply Chain Router</title>
  <link rel="stylesheet" href="/globals.css">
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/bundle.js"></script>
</body>
</html>
`
);
