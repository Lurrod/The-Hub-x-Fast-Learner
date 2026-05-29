module.exports = {
  apps: [
    {
      name: 'fl-bot',
      script: 'bot.py',
      interpreter: './venv/bin/python',
      cwd: '/home/ubuntu/fl-bot',
      autorestart: true,
      watch: false,
      max_restarts: 10,
      restart_delay: 5000,
      max_memory_restart: '500M',
      error_file: './logs/error.log',
      out_file: './logs/out.log',
      merge_logs: true,
      time: true,
      env: {
        PYTHONUNBUFFERED: '1',
      },
    },
  ],
};
