import { readFileSync } from "fs";
import { dirname, join } from "path";
import { fileURLToPath } from "url";

declare const Bun: {
  build: (cfg: Record<string, unknown>) => Promise<{
    success: boolean;
    outputs: Array<{ text: () => Promise<string> }>;
    logs: Array<{ message: string }>;
  }>;
  serve: (cfg: {
    port: number;
    fetch: (req: Request) => Promise<Response> | Response;
  }) => { port: number };
};

const PORT = Number(process.env.PORT || 3000);
const WEB_ROOT = dirname(fileURLToPath(import.meta.url));

async function buildBundle(): Promise<string> {
  const result = await Bun.build({
    entrypoints: [join(WEB_ROOT, "src", "client.tsx")],
    target: "browser",
    define: {
      "process.env.NEXT_PUBLIC_API_URL": JSON.stringify(
        process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"
      ),
      "process.env.NEXT_PUBLIC_MAPBOX_TOKEN": JSON.stringify(
        process.env.NEXT_PUBLIC_MAPBOX_TOKEN || ""
      )
    }
  });

  if (!result.success || result.outputs.length === 0) {
    const logs = result.logs.map((l) => l.message).join("\n");
    throw new Error(`Client bundle build failed:\n${logs}`);
  }
  return await result.outputs[0].text();
}

const cssContent = readFileSync(
  join(WEB_ROOT, "src", "app", "globals.css"),
  "utf-8"
);

const bundlePromise = buildBundle();

const htmlDocument = `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Flood-Resilient Supply Chain Router</title>
  <link rel="stylesheet" href="/globals.css" />
</head>
<body>
  <div id="root"></div>
  <script type="module" src="/bundle.js"></script>
</body>
</html>`;

const server = Bun.serve({
  port: PORT,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/globals.css") {
      return new Response(cssContent, {
        headers: { "Content-Type": "text/css; charset=utf-8" }
      });
    }
    if (url.pathname === "/bundle.js") {
      const js = await bundlePromise;
      return new Response(js, {
        headers: { "Content-Type": "application/javascript; charset=utf-8" }
      });
    }
    return new Response(htmlDocument, {
      headers: { "Content-Type": "text/html; charset=utf-8" }
    });
  }
});

console.log(`Flood-Router web frontend listening on http://localhost:${server.port}`);
