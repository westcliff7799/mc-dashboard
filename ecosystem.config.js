// PM2 process definitions.  Start with:  pm2 start ecosystem.config.js
//
// Two processes, not one. The dashboard is the thing you want up; the
// supervisor is what notices when it is up but no longer working.
module.exports = {
  apps: [
    {
      name: "mc-dashboard",
      cwd: "/home/westcliff7799/mc-dashboard-1",
      // interpreter "none" because uvicorn is a real executable with its own
      // shebang — letting PM2 hand it to node would just fail.
      script: "./.venv/bin/uvicorn",
      interpreter: "none",
      // --no-proxy-headers is load-bearing: uvicorn otherwise overwrites the
      // client address from the leftmost X-Forwarded-For, which the caller
      // controls, and the login lockout keys on that address. See README.
      args: "app.main:app --host 127.0.0.1 --port 8080 --no-proxy-headers",

      autorestart: true,
      // Code changes are handled by the supervisor instead, which checks the
      // new code imports before bouncing anything. PM2's own watcher would
      // restart straight into a half-finished save and leave you with nothing
      // running at all.
      watch: false,

      // Keep trying essentially forever rather than parking in "errored" at
      // 3am. The backoff is what stops a genuinely broken config from
      // spinning the CPU — PM2 widens the gap between attempts on its own.
      max_restarts: 10000,
      exp_backoff_restart_delay: 1000,
      min_uptime: "20s",

      // A leak should recycle the process, not the Pi.
      max_memory_restart: "300M",
      // Match the app's own shutdown budget so a socket that won't drain
      // can't hold the restart open.
      kill_timeout: 20000,

      merge_logs: true,
      time: true,
    },
    {
      name: "mc-dashboard-supervisor",
      cwd: "/home/westcliff7799/mc-dashboard-1",
      script: "./deploy/supervise.sh",
      interpreter: "bash",
      autorestart: true,
      watch: false,
      // It sleeps between ticks, so a "crash" here is real, not a fast exit.
      min_uptime: "60s",
      max_restarts: 10000,
      restart_delay: 5000,
      env: {
        TARGET_APP: "mc-dashboard",
        APP_DIR: "/home/westcliff7799/mc-dashboard-1",
        HEALTH_URL: "http://127.0.0.1:8080/healthz",
        // Worst case to recovery is FAIL_THRESHOLD * (PROBE_TIMEOUT + INTERVAL).
        INTERVAL: "10",
        PROBE_TIMEOUT: "5",
        FAIL_THRESHOLD: "3",
        // Set to 0 to keep the liveness probe but stop restarting on edits.
        WATCH_CODE: "1",
      },
      merge_logs: true,
      time: true,
    },
  ],
};
