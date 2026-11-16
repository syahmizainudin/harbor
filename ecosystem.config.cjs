// Keeps harbor running in the background with pm2:  pm2 start ecosystem.config.cjs && pm2 save
// Settings come from .env in this folder (or ~/.config/harbor/.env); see .env.example.
module.exports = {
  apps: [{
    name: "harbor",
    script: "bin/harbor",
    args: "ui",
    cwd: __dirname,
    interpreter: "python3",
    // Pinned cbz-webp and dl-queue, installed with: pip install --target .deps .
    env: { PYTHONPATH: __dirname + "/.deps", PYTHONUNBUFFERED: "1" },
    autorestart: true,
    max_restarts: 10,
    restart_delay: 3000,
    // Time to save unfinished jobs and stop worker threads before pm2 force-kills.
    kill_timeout: 15000,
  }],
};
