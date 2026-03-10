const express = require("express");

const app = express();

const os = require("os");
const SERVER_ID = process.env.SERVER_ID || os.hostname();
const PORT = process.env.PORT || 8080;

app.get("/", (req, res) => {
  res.send(`Hello from ${SERVER_ID}`);
});

app.get("/health", (req, res) => {
  res.json({
    status: "healthy",
    server: SERVER_ID
  });
});

app.listen(PORT, () => {
  console.log(`${SERVER_ID} running on port ${PORT}`);
});