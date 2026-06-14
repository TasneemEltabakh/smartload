import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./ui/tokens.css";
import "./views/base.css";
import "./styles.css";
import { getInitialTheme, setTheme } from "./ui";

// Resolve the theme at boot: a persisted choice, else the OS preference, else
// the product default (Daylight / light). Applied before first paint so the
// chosen palette is in place from the start.
setTheme(getInitialTheme());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
