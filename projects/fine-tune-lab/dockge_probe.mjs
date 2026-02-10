#!/usr/bin/env node
/**
 * Probe Dockge to understand auth state and available events.
 */
import { io } from "socket.io-client";

const DOCKGE_URL = "http://192.168.53.190:5001";

const socket = io(DOCKGE_URL, {
  reconnection: false,
  timeout: 10000,
  transports: ["websocket", "polling"],
});

socket.on("connect", () => {
  console.log("Connected, socket id:", socket.id);
});

// Listen for ALL events
socket.onAny((event, ...args) => {
  console.log(`EVENT: ${event}`, JSON.stringify(args).substring(0, 500));
});

socket.on("info", (data) => {
  console.log("INFO event:", JSON.stringify(data, null, 2));
});

socket.on("needSetup", (data) => {
  console.log("NEED SETUP:", data);
});

socket.on("connect_error", (err) => {
  console.error("Connection error:", err.message);
});

// Try setup with new credentials if needSetup is true
setTimeout(() => {
  console.log("\nTrying to check needSetup...");
  // Try to get info without auth
  socket.emit("needSetup", (res) => {
    console.log("needSetup response:", res);
  });
}, 2000);

// Try login with common defaults
setTimeout(() => {
  console.log("\nTrying login with admin/admin...");
  socket.emit("login", { username: "admin", password: "admin" }, (res) => {
    console.log("Login response:", JSON.stringify(res));
  });
}, 3000);

setTimeout(() => {
  socket.disconnect();
  process.exit(0);
}, 8000);
