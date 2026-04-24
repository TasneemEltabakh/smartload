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

/*
---------------------------------------------------
Request Logging Middleware
Logs every request in structured JSON format
---------------------------------------------------
*/
app.use((req, res, next) => {
  const start = Date.now();

  res.on("finish", () => {
    const log = {
      timestamp: new Date().toISOString(),
      service: "backend",
      server: SERVER_ID,
      method: req.method,
      path: req.originalUrl,
      status: res.statusCode,
      latency_ms: Date.now() - start,
      client_ip: req.ip
    };

    console.log(JSON.stringify(log));
  });

  next();
});

/*
---------------------------------------------------
Main Route
---------------------------------------------------
*/
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

/*
---------------------------------------------------
Health Check Endpoint
---------------------------------------------------
*/
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

/*
---------------------------------------------------
Start Server
---------------------------------------------------
*/
app.listen(PORT, () => {

  console.log(JSON.stringify({
    event: "server_started",
    server: SERVER_ID,
    port: PORT,
    timestamp: new Date().toISOString()
  }));

});