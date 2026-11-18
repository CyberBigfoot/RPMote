"""Common cron administration commands and editable, copy-only job examples.

Syntax reference: https://man7.org/linux/man-pages/man5/crontab.5.html
These are examples, not a measured popularity ranking. Scheduled entries
must go through crontab, never be sent to a shell as executable commands.
"""

from __future__ import annotations


def get_cron_command_library():
    commands = []
    def command(name, text, description):
        commands.append({"category": "Cron management", "name": name,
                         "command": text, "description": description})
    def job(name, schedule, text, description, root=False):
        commands.append({"category": "Cron jobs", "name": name,
                         "command": f"{schedule} {text}", "description": description,
                         "copy_only": True, "requires_root": root})

    command("List my cron jobs", "crontab -l", "Show the current user's crontab.")
    command("Edit my cron jobs", "crontab -e", "Open the current user's crontab in the remote terminal editor.")
    command("List root cron jobs", "sudo crontab -l", "Show root's crontab; requires sudo rights.")
    command("Edit root cron jobs", "sudo crontab -e", "Edit root's scheduled tasks; requires sudo rights.")
    command("Back up my crontab", 'crontab -l > "$HOME/crontab-backup-$(date +%Y%m%d-%H%M%S).txt"', "Save the current crontab before changing it. Check the command's exit status.")
    command("Show cron service status", "systemctl status cron --no-pager || systemctl status crond --no-pager", "Check Debian/Ubuntu cron or RHEL/Fedora crond on a systemd host.")
    command("Show recent cron logs", "sudo journalctl -u cron -u crond --since '24 hours ago' --no-pager", "Review the last day's cron service messages on systemd hosts.")
    command("Show traditional cron log", "sudo tail -n 100 /var/log/cron", "Common on RHEL-family systems; log location depends on the host.")
    command("Show system crontab", "cat /etc/crontab", "System crontabs include a username field after the five time fields.")
    command("List system cron folders", "ls -lah /etc/cron.d /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly", "Inspect distro-managed schedules before creating duplicate jobs.")
    command("Show systemd timers", "systemctl list-timers --all --no-pager", "Check for existing renewal, update, or cleanup timers.")
    command("Show server time and timezone", "date; timedatectl status", "Cron normally follows the remote server's timezone. timedatectl requires systemd.")

    # Frequently used schedule patterns.
    for name, schedule, description in (
        ("Run a script every minute", "* * * * *", "Every minute; choose a short-running script."),
        ("Run a script every 5 minutes", "*/5 * * * *", "At minutes 0, 5, 10, and so on."),
        ("Run a script every 15 minutes", "*/15 * * * *", "At minutes 0, 15, 30, and 45."),
        ("Run a script every 30 minutes", "*/30 * * * *", "At minutes 0 and 30."),
        ("Run a script hourly", "0 * * * *", "At the top of each hour."),
        ("Run a script daily at 2 AM", "0 2 * * *", "Daily at 02:00 in the server's timezone."),
        ("Run a script on weekdays at 8 AM", "0 8 * * 1-5", "Monday through Friday at 08:00."),
        ("Run a script weekly on Sunday", "0 3 * * 0", "Sunday at 03:00."),
        ("Run a script on the first of each month", "0 3 1 * *", "The first day of the month at 03:00."),
    ):
        job(name, schedule, '/path/to/script.sh >> "$HOME/cron-job.log" 2>&1', description + " Replace the script path and choose a writable log.")

    job("Run a startup script after reboot", "@reboot", '/bin/sleep 30 && /path/to/startup.sh >> "$HOME/startup.log" 2>&1', "After the cron daemon starts, wait 30 seconds then run a script. Available on common cron implementations; this does not guarantee network readiness.")
    job("Nightly compressed application backup", "0 2 * * *", 'mkdir -p "$HOME/backups" && /usr/bin/tar -czf "$HOME/backups/app-$(/usr/bin/date +\\%F).tar.gz" -C /srv app >> "$HOME/backups/backup.log" 2>&1', "Back up /srv/app at 02:00. Replace the source and destination; ensure space and read permissions. Literal percent signs are escaped for cron.")
    job("Nightly rsync backup", "30 2 * * *", '/usr/bin/rsync -a /srv/app/ /mnt/backup/app/ >> "$HOME/rsync-backup.log" 2>&1', "Copy an application directory to an already mounted backup destination. No destination files are deleted; replace both paths.")
    job("Nightly PostgreSQL database backup", "0 2 * * *", 'mkdir -p "$HOME/backups" && /usr/bin/pg_dump -Fc database_name > "$HOME/backups/postgres-$(/usr/bin/date +\\%F).dump" 2>> "$HOME/backups/postgres-backup.log"', "Replace database_name; configure .pgpass or another non-interactive login. The job runs as the crontab owner.")
    job("Nightly MySQL database backup", "15 2 * * *", 'mkdir -p "$HOME/backups" && /usr/bin/mysqldump --defaults-extra-file="$HOME/.my-backup.cnf" --single-transaction database_name > "$HOME/backups/mysql-$(/usr/bin/date +\\%F).sql" 2>> "$HOME/backups/mysql-backup.log"', "Replace database_name. Configure a protected defaults file for credentials; --single-transaction is intended for transactional tables.")
    job("Website health check every 5 minutes", "*/5 * * * *", '/usr/bin/curl --fail --silent --show-error --max-time 30 --output /dev/null https://example.com/health >> "$HOME/website-health.log" 2>&1', "Check an HTTP health endpoint. Failures go to the log; configure notification separately.")
    job("Record disk usage hourly", "0 * * * *", '/bin/df -h >> "$HOME/disk-usage.log" 2>&1', "Append filesystem usage to a writable log. Add log rotation if kept long-term.")
    job("Record system load every 5 minutes", "*/5 * * * *", '/usr/bin/uptime >> "$HOME/system-load.log" 2>&1', "Keep a simple load and uptime history in a writable log.")
    job("Preview backups older than 30 days", "0 4 * * *", '/usr/bin/find "$HOME/backups" -type f -name "*.tar.gz" -mtime +30 -print >> "$HOME/old-backups.log" 2>&1', "List old tar backups so you can review a retention policy before enabling deletion.")
    job("Delete application cache files older than 7 days", "0 4 * * *", '/usr/bin/find /path/to/app/cache -xdev -type f -mtime +7 -delete >> "$HOME/cache-cleanup.log" 2>&1', "DELETES files in the chosen cache directory. Review the path and first test the same command with -print instead of -delete.")
    job("Compress old application logs weekly", "0 4 * * 0", '/usr/bin/find /path/to/app/logs -xdev -type f -name "*.log" -mtime +7 -exec /usr/bin/gzip -- {} + >> "$HOME/log-compression.log" 2>&1', "Compress old, inactive logs in a chosen application folder. Use the application's log rotation policy for active logs.")
    job("Run a custom logrotate policy daily", "0 4 * * *", '/usr/sbin/logrotate /path/to/logrotate.conf', "Use a reviewed custom configuration. Check existing cron/systemd logrotate schedules first; needs root for protected logs.", root=True)
    job("Renew Let's Encrypt certificates", "17 3,15 * * *", '/usr/bin/certbot renew --quiet', "Run twice daily only if Certbot has no existing renewal timer or cron job. Verify installation path and renewal/deploy hooks.", root=True)
    job("Refresh apt package indexes daily", "0 5 * * *", '/usr/bin/apt-get update >> /var/log/apt-cron-update.log 2>&1', "Debian/Ubuntu example: refresh package indexes as root. Does not install upgrades. Check existing apt timers first.", root=True)
    job("Run a Python script daily", "0 1 * * *", '/path/to/venv/bin/python /path/to/script.py >> "$HOME/python-job.log" 2>&1', "Use the full interpreter path, including a virtual environment when needed. Replace both paths.")
    job("Laravel task scheduler every minute", "* * * * *", 'cd /var/www/app && /usr/bin/php artisan schedule:run >> "$HOME/laravel-scheduler.log" 2>&1', "Laravel's scheduler entry point. Replace the application and PHP paths; run as the appropriate application user.")
    job("WordPress due cron events every 5 minutes", "*/5 * * * *", '/usr/local/bin/wp --path=/var/www/site cron event run --due-now >> "$HOME/wp-cron.log" 2>&1', "WP-CLI example. Replace paths and coordinate with the site's WP-Cron configuration to avoid duplicate scheduling.")
    job("Prevent overlapping scheduled runs with flock", "*/5 * * * *", '/usr/bin/flock -n "$HOME/.rpmote-job.lock" /path/to/job.sh >> "$HOME/locked-job.log" 2>&1', "Skip a run if the previous job still holds the lock. Verify flock is installed and replace the script path.")
    job("Limit a scheduled job to 10 minutes", "*/15 * * * *", '/usr/bin/timeout 10m /path/to/job.sh >> "$HOME/timed-job.log" 2>&1', "GNU timeout stops a job that exceeds 10 minutes. Ensure the job can be interrupted safely.")
    return commands
