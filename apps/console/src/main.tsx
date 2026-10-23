import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// Global styles and tokens first: component and layout CSS (imported by App) may
// override tokens inside media queries, which only works if they load later.
import "./styles/global.css";
import { App } from "./App";

const root = document.getElementById("root");
if (!root) throw new Error("PexisOS Console: #root element missing");
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
