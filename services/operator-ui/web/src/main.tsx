import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import App from "./App";
import "./ui/tokens.css";
import "./views/base.css";
import { setTheme } from "./ui";

// Operator UI ships with the Daylight (light) theme by default.
setTheme("light");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </React.StrictMode>,
);
