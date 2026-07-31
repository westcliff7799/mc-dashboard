module.exports = {
  apps: [
    {
      name: "mc-dashboard",
      cwd: "/home/westcliff7799/mc-dashboard-1",
      script: "./.venv/bin/uvicorn",
      interpreter: "none",
      args: "app.main:app --host 127.0.0.1 --port 8080 --no-proxy-headers",

      autorestart: true,
      watch: false,

      max_restarts: 10000,
      exp_backoff_restart_delay: 1000,
      min_uptime: "20s",

      max_memory_restart: "300M",
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
      min_uptime: "60s",
      max_restarts: 10000,
      restart_delay: 5000,
      env: {
        TARGET_APP: "mc-dashboard",
        APP_DIR: "/home/westcliff7799/mc-dashboard-1",
        HEALTH_URL: "http://127.0.0.1:8080/healthz",
        INTERVAL: "10",
        PROBE_TIMEOUT: "5",
        FAIL_THRESHOLD: "3",
        WATCH_CODE: "1",
      },
      merge_logs: true,
      time: true,
    },
  ],
};
