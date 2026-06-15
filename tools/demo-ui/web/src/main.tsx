import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./ui/tokens.css";
import "./base.css";
import { setTheme } from "./ui";

// Presentation surface for a thesis defense: light, high-contrast, projector-
// legible theme is the default and the primary design target.
setTheme("light");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
