import React from "react";
import { createRoot } from "react-dom/client";
import HomePage from "./app/page";
import EvidencePage from "./app/EvidencePage";

const container = document.getElementById("root");
if (container) {
  const root = createRoot(container);
  root.render(
    <React.StrictMode>
      {window.location.pathname === "/evidence" ? <EvidencePage /> : <HomePage />}
    </React.StrictMode>
  );
}
