#!/usr/bin/env node
/**
 * Deploy fine-tune-lab to 3090 via Dockge Socket.IO API.
 *
 * Usage: node deploy_dockge.mjs [login|deploy|status|up|down]
 *
 * Dockge API uses Socket.IO with events like:
 * - login(username, password, token, callback)
 * - saveStack(name, composeYAML, composeENV, isAdd, callback)
 * - deployStack(name, callback)
 * - getStackList(callback)
 * - getStackStatus(name, callback)
 */

import { io } from "socket.io-client";

const DOCKGE_URL = process.env.DOCKGE_URL || "http://192.168.53.190:5001";
const DOCKGE_USER = process.env.DOCKGE_USER || "";
const DOCKGE_PASS = process.env.DOCKGE_PASS || "";
const STACK_NAME = "fine-tune-lab";

// The compose YAML that Dockge will use - builds from GitHub
const COMPOSE_YAML = `
services:
  fine-tune-lab:
    build:
      context: https://github.com/Nox-forge/aispace.git#master:projects/fine-tune-lab
      dockerfile: Dockerfile
    container_name: fine-tune-lab
    restart: unless-stopped
    ports:
      - "8881:8881"
    environment:
      - OLLAMA_URL=http://host.docker.internal:8080
      - BASE_MODEL=llama3.2:3b
      - HF_BASE_MODEL=unsloth/Llama-3.2-3B-Instruct
      - NVIDIA_VISIBLE_DEVICES=all
      - HF_HOME=/data/hf_cache
    volumes:
      - finetune-data:/data
      - hf-cache:/data/hf_cache
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
    extra_hosts:
      - "host.docker.internal:host-gateway"

volumes:
  finetune-data:
  hf-cache:
`.trim();

function connect() {
  return new Promise((resolve, reject) => {
    const socket = io(DOCKGE_URL, {
      reconnection: false,
      timeout: 10000,
      transports: ["websocket", "polling"],
    });

    socket.on("connect", () => {
      console.log("Connected to Dockge");
      resolve(socket);
    });

    socket.on("connect_error", (err) => {
      console.error("Connection failed:", err.message);
      reject(err);
    });

    setTimeout(() => reject(new Error("Connection timeout")), 15000);
  });
}

function login(socket) {
  return new Promise((resolve, reject) => {
    if (!DOCKGE_USER) {
      // Try without auth first
      console.log("No credentials provided, trying without auth...");
      resolve(true);
      return;
    }

    socket.emit("login", {
      username: DOCKGE_USER,
      password: DOCKGE_PASS,
      token: "",
    }, (res) => {
      if (res.ok) {
        console.log("Logged in successfully");
        resolve(true);
      } else {
        console.error("Login failed:", res.msg);
        reject(new Error(res.msg));
      }
    });
  });
}

function listStacks(socket) {
  return new Promise((resolve) => {
    // Dockge sends stack list via events after connection
    const stacks = {};
    socket.on("stackList", (data) => {
      Object.assign(stacks, data);
    });
    // Give it a moment to receive
    setTimeout(() => resolve(stacks), 2000);
  });
}

function saveStack(socket) {
  return new Promise((resolve, reject) => {
    socket.emit("saveStack", STACK_NAME, COMPOSE_YAML, "", true, (res) => {
      if (res && res.ok) {
        console.log("Stack saved successfully");
        resolve(true);
      } else {
        console.error("Save failed:", res?.msg || "unknown error");
        // Try update instead of add
        socket.emit("saveStack", STACK_NAME, COMPOSE_YAML, "", false, (res2) => {
          if (res2 && res2.ok) {
            console.log("Stack updated successfully");
            resolve(true);
          } else {
            reject(new Error(res2?.msg || "Save failed"));
          }
        });
      }
    });
  });
}

function deployStack(socket) {
  return new Promise((resolve, reject) => {
    console.log("Deploying stack (this will build the image, may take 10-20 minutes)...");
    socket.emit("deployStack", STACK_NAME, (res) => {
      if (res && res.ok) {
        console.log("Deploy initiated successfully");
        resolve(true);
      } else {
        console.error("Deploy failed:", res?.msg || "unknown error");
        reject(new Error(res?.msg || "Deploy failed"));
      }
    });
  });
}

function upStack(socket) {
  return new Promise((resolve, reject) => {
    socket.emit("startStack", STACK_NAME, (res) => {
      if (res && res.ok) {
        console.log("Stack started");
        resolve(true);
      } else {
        reject(new Error(res?.msg || "Start failed"));
      }
    });
  });
}

function downStack(socket) {
  return new Promise((resolve, reject) => {
    socket.emit("stopStack", STACK_NAME, (res) => {
      if (res && res.ok) {
        console.log("Stack stopped");
        resolve(true);
      } else {
        reject(new Error(res?.msg || "Stop failed"));
      }
    });
  });
}

async function main() {
  const action = process.argv[2] || "deploy";

  try {
    const socket = await connect();
    await login(socket);

    switch (action) {
      case "status":
        const stacks = await listStacks(socket);
        console.log("Stacks:", JSON.stringify(stacks, null, 2));
        break;

      case "save":
        await saveStack(socket);
        break;

      case "deploy":
        await saveStack(socket);
        await deployStack(socket);
        break;

      case "up":
        await upStack(socket);
        break;

      case "down":
        await downStack(socket);
        break;

      default:
        console.log("Usage: node deploy_dockge.mjs [status|save|deploy|up|down]");
    }

    setTimeout(() => {
      socket.disconnect();
      process.exit(0);
    }, 3000);

  } catch (err) {
    console.error("Error:", err.message);
    process.exit(1);
  }
}

main();
