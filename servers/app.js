const express = require("express");
const os = require("os");

const app = express();

const SERVER_ID = process.env.SERVER_ID || os.hostname();
const PORT = process.env.PORT || 8080;

// Optional knobs for demo/testing
const RESPONSE_DELAY_MS = Number(process.env.RESPONSE_DELAY_MS || 0);
const FAIL_HEALTH = process.env.FAIL_HEALTH === "true";
const FAIL_ALL = process.env.FAIL_ALL === "true";

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

app.get("/", async (req, res) => {
  if (RESPONSE_DELAY_MS > 0) {
    await sleep(RESPONSE_DELAY_MS);
  }

  if (FAIL_ALL) {
    return res.status(503).json({
      status: "unavailable",
      server: SERVER_ID
    });
  }

  res.send(`Hello from ${SERVER_ID}`);
});

app.get("/health", async (req, res) => {
  if (RESPONSE_DELAY_MS > 0) {
    await sleep(RESPONSE_DELAY_MS);
  }

  if (FAIL_HEALTH || FAIL_ALL) {
    return res.status(503).json({
      status: "unhealthy",
      server: SERVER_ID
    });
  }

  res.json({
    status: "healthy",
    server: SERVER_ID
  });
});

app.listen(PORT, () => {
  console.log(`${SERVER_ID} running on port ${PORT}`);
});