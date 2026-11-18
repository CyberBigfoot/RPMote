"""
Curated Bash command library for quick execution from the UI.
"""

from __future__ import annotations


def _c(category: str, name: str, command: str, description: str = "") -> dict:
    return {
        "category": category,
        "name": name,
        "command": command,
        "description": description,
    }


def get_bash_command_library() -> list[dict]:
    commands: list[dict] = [
        # Navigation & Files
        _c("Navigation", "Print working directory", "pwd", "Show current path"),
        _c("Navigation", "List files (long)", "ls -lah", "Detailed directory listing"),
        _c("Navigation", "List only directories", "ls -d */", "Show folders only"),
        _c("Navigation", "Tree (2 levels)", "tree -L 2", "Directory tree overview"),
        _c("Navigation", "Go home", "cd ~", "Jump to home directory"),
        _c("Navigation", "Go previous directory", "cd -", "Toggle previous directory"),
        _c("Navigation", "Disk usage current dir", "du -sh .", "Total size of current directory"),
        _c("Navigation", "Top 20 largest entries", "du -ah . | sort -rh | head -20", "Largest files/folders"),
        _c("Navigation", "Show mount points", "mount | column -t", "Mounted filesystems"),
        _c("Navigation", "Filesystem usage", "df -h", "Disk free space"),

        # File operations
        _c("Files", "Create file", "touch file.txt", "Create empty file"),
        _c("Files", "Create directory", "mkdir -p new_folder/sub", "Create nested directories"),
        _c("Files", "Copy recursively", "cp -r src_dir dst_dir", "Copy directory tree"),
        _c("Files", "Move/rename", "mv old_name new_name", "Move or rename path"),
        _c("Files", "Remove recursively", "rm -rf target_path", "Delete path recursively"),
        _c("Files", "Safe remove prompt", "rm -ri target_path", "Prompt before deleting"),
        _c("Files", "Symlink file", "ln -s /path/to/source /path/to/link", "Create symbolic link"),
        _c("Files", "Hard link file", "ln /path/to/source /path/to/link", "Create hard link"),
        _c("Files", "Change owner recursively", "chown -R user:group /path", "Set ownership"),
        _c("Files", "Change permissions recursively", "chmod -R 755 /path", "Set permissions"),

        # Viewing / Text
        _c("Text", "Show first 50 lines", "head -n 50 file.log", "Preview file start"),
        _c("Text", "Show last 100 lines", "tail -n 100 file.log", "Preview file end"),
        _c("Text", "Follow log live", "tail -f file.log", "Stream log updates"),
        _c("Text", "Page through file", "less file.log", "Open in pager"),
        _c("Text", "Word count", "wc -lwm file.txt", "Lines words chars"),
        _c("Text", "Sort unique", "sort file.txt | uniq", "Sort then deduplicate"),
        _c("Text", "Count unique lines", "sort file.txt | uniq -c | sort -nr", "Top repeated lines"),
        _c("Text", "Join lines with comma", "paste -sd, file.txt", "CSV from lines"),
        _c("Text", "Cut first field by colon", "cut -d: -f1 /etc/passwd", "Extract first column"),
        _c("Text", "Reverse lines", "tac file.txt", "Print lines in reverse"),

        # Search
        _c("Search", "Find by name", "find . -type f -name '*.log'", "Locate files by pattern"),
        _c("Search", "Find case-insensitive", "find . -type f -iname '*readme*'", "Case-insensitive filename search"),
        _c("Search", "Find empty files", "find . -type f -empty", "Locate empty files"),
        _c("Search", "Find large files", "find . -type f -size +100M", "Files bigger than 100MB"),
        _c("Search", "Find recently modified", "find . -type f -mtime -1", "Modified in last 24h"),
        _c("Search", "Grep recursively", "grep -RIn --color=always 'pattern' .", "Recursive text search"),
        _c("Search", "Grep whole word", "grep -RInw 'pattern' .", "Match whole words"),
        _c("Search", "Exclude dirs in grep", "grep -RIn --exclude-dir={.git,node_modules,dist} 'pattern' .", "Skip noisy directories"),
        _c("Search", "Find executable files", "find . -type f -perm -111", "Locate executables"),
        _c("Search", "Locate command path", "command -v python3", "Resolve executable path"),

        # Process / System
        _c("System", "Show processes", "ps aux --sort=-%cpu | head -20", "Top CPU processes"),
        _c("System", "Interactive process monitor", "top", "Live process table"),
        _c("System", "Modern process monitor", "htop", "Interactive process viewer"),
        _c("System", "Kill by PID", "kill -15 <pid>", "Graceful terminate"),
        _c("System", "Force kill by PID", "kill -9 <pid>", "Hard terminate"),
        _c("System", "Kill by process name", "pkill -f '<name>'", "Kill matching process"),
        _c("System", "System uptime", "uptime", "Load and uptime"),
        _c("System", "Kernel and distro", "uname -a && cat /etc/os-release", "OS details"),
        _c("System", "Memory usage", "free -h", "RAM usage"),
        _c("System", "Open listening ports", "ss -tulpn", "Network listeners"),

        # Networking
        _c("Networking", "IP addresses", "ip a", "Show interfaces and IPs"),
        _c("Networking", "Routing table", "ip route", "Show routes"),
        _c("Networking", "Ping host", "ping -c 4 example.com", "Connectivity test"),
        _c("Networking", "DNS lookup", "dig +short example.com", "Resolve DNS"),
        _c("Networking", "Trace route", "traceroute example.com", "Path to host"),
        _c("Networking", "HTTP headers", "curl -I https://example.com", "Fetch response headers"),
        _c("Networking", "Download file", "curl -L -o output.bin https://example.com/file.bin", "Download URL"),
        _c("Networking", "POST JSON", "curl -X POST -H 'Content-Type: application/json' -d '{\"ok\":true}' https://example.com/api", "Send JSON payload"),
        _c("Networking", "Check TLS cert", "echo | openssl s_client -connect example.com:443 -servername example.com 2>/dev/null | openssl x509 -noout -dates -subject", "Inspect certificate"),
        _c("Networking", "Test TCP port", "nc -zv example.com 443", "Port connectivity"),

        # Archives
        _c("Archives", "Create tar.gz", "tar -czf archive.tar.gz folder/", "Compress directory"),
        _c("Archives", "Extract tar.gz", "tar -xzf archive.tar.gz", "Extract gzip tarball"),
        _c("Archives", "Create tar.xz", "tar -cJf archive.tar.xz folder/", "High-compression tarball"),
        _c("Archives", "Extract tar.xz", "tar -xJf archive.tar.xz", "Extract xz tarball"),
        _c("Archives", "List tar contents", "tar -tf archive.tar.gz", "Preview archive"),
        _c("Archives", "Create zip", "zip -r archive.zip folder/", "Zip folder"),
        _c("Archives", "Extract zip", "unzip archive.zip -d out_dir", "Unzip archive"),
        _c("Archives", "Gzip file", "gzip file.log", "Compress single file"),
        _c("Archives", "Gunzip file", "gunzip file.log.gz", "Decompress gzip"),
        _c("Archives", "Split file 100MB", "split -b 100M bigfile.bin part_", "Split large file"),

        # SSH / Remote
        _c("SSH", "SSH default", "ssh user@host", "Connect over SSH"),
        _c("SSH", "SSH with key", "ssh -i ~/.ssh/id_ed25519 user@host", "Connect with private key"),
        _c("SSH", "SSH custom port", "ssh -p 2222 user@host", "Connect on custom port"),
        _c("SSH", "Copy file to remote", "scp local.file user@host:/remote/path/", "Upload file"),
        _c("SSH", "Copy file from remote", "scp user@host:/remote/file ./", "Download file"),
        _c("SSH", "Sync folder with rsync", "rsync -avz --progress ./local/ user@host:/remote/", "Efficient folder sync"),
        _c("SSH", "Remote command", "ssh user@host 'hostname && uptime'", "Run remote command"),
        _c("SSH", "SSH tunnel local->remote", "ssh -L 8080:127.0.0.1:80 user@host", "Local port forward"),
        _c("SSH", "SSH reverse tunnel", "ssh -R 9000:127.0.0.1:22 user@host", "Reverse port forward"),
        _c("SSH", "Generate SSH key", "ssh-keygen -t ed25519 -a 100 -f ~/.ssh/id_ed25519", "Create secure keypair"),

        # Git
        _c("Git", "Git status short", "git status -sb", "Compact status"),
        _c("Git", "Git pull rebase", "git pull --rebase", "Update branch cleanly"),
        _c("Git", "Git add all", "git add -A", "Stage all changes"),
        _c("Git", "Git commit", "git commit -m 'message'", "Commit staged changes"),
        _c("Git", "Git push", "git push", "Push current branch"),
        _c("Git", "Git log graph", "git log --oneline --graph --decorate -n 30", "Recent commit graph"),
        _c("Git", "Git diff unstaged", "git diff", "Show unstaged changes"),
        _c("Git", "Git diff staged", "git diff --cached", "Show staged changes"),
        _c("Git", "Git stash push", "git stash push -u -m 'wip'", "Stash including untracked"),
        _c("Git", "Git stash pop", "git stash pop", "Apply latest stash"),

        # Docker
        _c("Docker", "List containers", "docker ps -a", "All containers"),
        _c("Docker", "List images", "docker images", "All images"),
        _c("Docker", "Follow container logs", "docker logs -f <container>", "Tail container logs"),
        _c("Docker", "Exec shell in container", "docker exec -it <container> bash", "Open container shell"),
        _c("Docker", "Build image", "docker build -t myapp:latest .", "Build from Dockerfile"),
        _c("Docker", "Run container", "docker run -d --name myapp -p 8080:80 myapp:latest", "Start container"),
        _c("Docker", "Stop all running", "docker stop $(docker ps -q)", "Stop running containers"),
        _c("Docker", "Remove exited containers", "docker container prune -f", "Cleanup containers"),
        _c("Docker", "Remove dangling images", "docker image prune -f", "Cleanup images"),
        _c("Docker", "Compose up", "docker compose up -d", "Start compose stack"),
        _c("Docker", "Compose down", "docker compose down", "Stop and remove compose stack"),
        _c("Docker", "Compose ps", "docker compose ps", "List compose services"),
        _c("Docker", "Compose pull", "docker compose pull", "Pull latest service images"),
        _c("Docker", "Compose build", "docker compose build --no-cache", "Rebuild compose images"),
        _c("Docker", "Compose config", "docker compose config", "Render effective compose config"),
        _c("Docker", "Container inspect", "docker inspect <container>", "Inspect container JSON metadata"),
        _c("Docker", "Image inspect", "docker image inspect <image>", "Inspect image metadata"),
        _c("Docker", "Container stats", "docker stats", "Live CPU/memory/net usage"),
        _c("Docker", "Image history", "docker history <image>", "Show image layers"),
        _c("Docker", "System df", "docker system df", "Show Docker disk usage"),
        _c("Docker", "System prune", "docker system prune -af --volumes", "Aggressive Docker cleanup"),
        _c("Docker", "Network list", "docker network ls", "List Docker networks"),
        _c("Docker", "Volume list", "docker volume ls", "List Docker volumes"),
        _c("Docker", "Prune unused volumes", "docker volume prune -f", "Remove dangling volumes"),
        _c("Docker", "Tag image", "docker tag source:tag target:tag", "Create image tag"),
        _c("Docker", "Push image", "docker push repo/image:tag", "Push image to registry"),
        _c("Docker", "Pull image", "docker pull repo/image:tag", "Pull image from registry"),
        _c("Docker", "Save image tar", "docker save -o image.tar repo/image:tag", "Export image to tar"),
        _c("Docker", "Load image tar", "docker load -i image.tar", "Import image from tar"),
        _c("Docker", "Copy from container", "docker cp <container>:/path/in/container ./local", "Copy file from container"),
        _c("Docker", "Copy into container", "docker cp ./local.file <container>:/path/in/container", "Copy file to container"),

        # Kubernetes
        _c("Kubernetes", "Current context", "kubectl config current-context", "Show active context"),
        _c("Kubernetes", "Get namespaces", "kubectl get ns", "List namespaces"),
        _c("Kubernetes", "Get pods", "kubectl get pods -A", "List all pods"),
        _c("Kubernetes", "Describe pod", "kubectl describe pod <pod> -n <ns>", "Inspect pod"),
        _c("Kubernetes", "Pod logs", "kubectl logs -f <pod> -n <ns>", "Follow pod logs"),
        _c("Kubernetes", "Exec into pod", "kubectl exec -it <pod> -n <ns> -- /bin/sh", "Open pod shell"),
        _c("Kubernetes", "Get services", "kubectl get svc -A", "List services"),
        _c("Kubernetes", "Get deployments", "kubectl get deploy -A", "List deployments"),
        _c("Kubernetes", "Rollout status", "kubectl rollout status deploy/<name> -n <ns>", "Deployment rollout state"),
        _c("Kubernetes", "Top pods", "kubectl top pods -A", "Resource usage by pod"),

        # Package managers
        _c("Packages", "APT update", "sudo apt update", "Refresh apt indexes"),
        _c("Packages", "APT upgrade", "sudo apt upgrade -y", "Upgrade installed packages"),
        _c("Packages", "APT install", "sudo apt install -y <package>", "Install package"),
        _c("Packages", "DNF update", "sudo dnf update -y", "Update Fedora/RHEL packages"),
        _c("Packages", "YUM install", "sudo yum install -y <package>", "Install package with yum"),
        _c("Packages", "Pacman sync", "sudo pacman -Syu", "Update Arch packages"),
        _c("Packages", "Pip install", "python3 -m pip install <package>", "Install Python package"),
        _c("Packages", "Pip upgrade all", "python3 -m pip list --outdated --format=freeze | cut -d '=' -f 1 | xargs -n1 python3 -m pip install -U", "Upgrade outdated pip packages"),
        _c("Packages", "NPM install", "npm install", "Install node deps"),
        _c("Packages", "NPM audit fix", "npm audit fix", "Fix vulnerable node deps"),

        # Service management
        _c("Services", "Systemctl status", "sudo systemctl status <service>", "Service status"),
        _c("Services", "Systemctl restart", "sudo systemctl restart <service>", "Restart service"),
        _c("Services", "Systemctl enable", "sudo systemctl enable <service>", "Enable on boot"),
        _c("Services", "Systemctl disable", "sudo systemctl disable <service>", "Disable on boot"),
        _c("Services", "List failed services", "systemctl --failed", "Show failed units"),
        _c("Services", "Journal latest 200", "journalctl -n 200 --no-pager", "Recent logs"),
        _c("Services", "Journal follow service", "journalctl -u <service> -f", "Live service logs"),
        _c("Services", "Crontab edit", "crontab -e", "Edit user cron jobs"),
        _c("Services", "Crontab list", "crontab -l", "List user cron jobs"),
        _c("Services", "At queue", "atq", "Show scheduled at jobs"),

        # Shell / Productivity
        _c("Shell", "Print environment", "env | sort", "Show sorted environment variables"),
        _c("Shell", "Show PATH on lines", "echo $PATH | tr ':' '\\n'", "Readable PATH"),
        _c("Shell", "Reload shell rc", "source ~/.bashrc", "Reload current shell config"),
        _c("Shell", "List aliases", "alias", "Show current aliases"),
        _c("Shell", "List shell functions", "declare -F", "Show available functions"),
        _c("Shell", "History latest 200", "history 200", "Show recent command history"),
        _c("Shell", "Top history commands", "history | awk '{print $2}' | sort | uniq -c | sort -nr | head -20", "Most used commands"),
        _c("Shell", "Run previous command as sudo", "sudo !!", "Repeat last command with sudo"),
        _c("Shell", "Measure command time", "time <command>", "Benchmark command execution"),
        _c("Shell", "Background command", "<command> &", "Run command in background"),

        # JSON / API helpers
        _c("JSON", "Pretty print JSON", "cat payload.json | jq .", "Format JSON output"),
        _c("JSON", "Extract field with jq", "jq -r '.field' payload.json", "Read JSON field"),
        _c("JSON", "List JSON keys", "jq 'keys' payload.json", "Show top-level keys"),
        _c("JSON", "Filter array items", "jq '.items[] | select(.enabled==true)' payload.json", "Filter JSON array"),
        _c("JSON", "Curl JSON pretty", "curl -s https://example.com/api | jq .", "Fetch and pretty-print JSON"),

        # Monitoring / diagnostics
        _c("Monitoring", "CPU info", "lscpu", "CPU architecture details"),
        _c("Monitoring", "Block devices", "lsblk -f", "Disks and filesystems"),
        _c("Monitoring", "I/O stats", "iostat -xz 1 5", "Disk IO profile"),
        _c("Monitoring", "VM stats", "vmstat 1 10", "Memory/process system stats"),
        _c("Monitoring", "Per-process open files", "lsof -p <pid>", "Files opened by process"),
        _c("Monitoring", "Top memory processes", "ps aux --sort=-%mem | head -20", "Top RAM consumers"),
        _c("Monitoring", "Dmesg recent", "dmesg | tail -n 100", "Kernel recent events"),
        _c("Monitoring", "Realtime network", "iftop", "Live bandwidth by connection"),
        _c("Monitoring", "TCP connections", "ss -tanp | head -50", "Active TCP sessions"),
        _c("Monitoring", "Port ownership", "sudo lsof -iTCP -sTCP:LISTEN -n -P", "Process bound to ports"),

        # User / session management
        _c("Users", "List logged users", "who", "Current logged-in users"),
        _c("Users", "User account info", "getent passwd <user>", "Inspect user entry"),
        _c("Users", "Create user", "sudo useradd -m -s /bin/bash <user>", "Add new user"),
        _c("Users", "Set user password", "sudo passwd <user>", "Set user password"),
        _c("Users", "Add user to group", "sudo usermod -aG <group> <user>", "Grant group membership"),
        _c("Users", "Lock user", "sudo usermod -L <user>", "Lock account"),
        _c("Users", "Unlock user", "sudo usermod -U <user>", "Unlock account"),
        _c("Users", "Show sudoers includes", "sudo ls -lah /etc/sudoers.d", "Inspect sudo include files"),
        _c("Users", "Current login sessions", "w", "Users + running commands"),
        _c("Users", "Last reboot records", "last reboot | head -20", "Reboot history"),
    ]

    # High-frequency grep/find patterns
    grep_patterns = [
        ("TODO", "TODO markers"),
        ("FIXME", "FIXME markers"),
        ("ERROR", "Error lines"),
        ("WARN", "Warning lines"),
        ("Exception", "Exceptions"),
        ("timeout", "Timeouts"),
        ("connection refused", "Connection refused"),
        ("denied", "Denied/permission issues"),
        ("deprecated", "Deprecated usage"),
        ("panic", "Panic events"),
        ("fatal", "Fatal events"),
        ("traceback", "Python traceback"),
    ]
    for pattern, label in grep_patterns:
        commands.append(_c("Search", f"Grep recursive: {label}", f"grep -RIn --color=always '{pattern}' .", label))
        commands.append(_c("Search", f"Grep logs: {label}", f"grep -RIn --color=always '{pattern}' ./*.log", label))

    find_patterns = [
        ("*.log", "Log files"),
        ("*.conf", "Config files"),
        ("*.service", "Systemd unit files"),
        ("*.sh", "Shell scripts"),
        ("*.py", "Python files"),
        ("*.js", "JavaScript files"),
        ("*.ts", "TypeScript files"),
        ("*.json", "JSON files"),
        ("*.yaml", "YAML files"),
        ("*.yml", "YML files"),
        ("*.env", "Environment files"),
        ("Dockerfile*", "Dockerfiles"),
    ]
    for pattern, label in find_patterns:
        commands.append(_c("Search", f"Find: {label}", f"find . -type f -name '{pattern}'", label))
        commands.append(_c("Search", f"Count: {label}", f"find . -type f -name '{pattern}' | wc -l", f"Count {label.lower()}"))

    # Git command variants
    git_branches = ["main", "master", "develop", "staging", "production", "release"]
    for b in git_branches:
        commands.append(_c("Git", f"Checkout {b}", f"git checkout {b}", f"Switch to {b}"))
        commands.append(_c("Git", f"Pull {b}", f"git pull origin {b}", f"Pull {b} from origin"))
        commands.append(_c("Git", f"Diff against {b}", f"git diff {b}...HEAD", f"Changes vs {b}"))

    # Docker useful variants
    docker_targets = ["web", "api", "worker", "db", "redis", "nginx"]
    for t in docker_targets:
        commands.append(_c("Docker", f"Compose logs {t}", f"docker compose logs -f {t}", f"Follow {t} logs"))
        commands.append(_c("Docker", f"Compose restart {t}", f"docker compose restart {t}", f"Restart {t}"))
        commands.append(_c("Docker", f"Compose up {t}", f"docker compose up -d {t}", f"Start only {t}"))

    docker_objects = ["container", "image", "network", "volume"]
    for obj in docker_objects:
        commands.append(_c("Docker", f"Prune {obj}s", f"docker {obj} prune -f", f"Prune unused {obj}s"))
        commands.append(_c("Docker", f"List {obj}s", f"docker {obj} ls", f"List Docker {obj}s"))

    # Kubernetes common entity filters
    k8s_namespaces = ["default", "kube-system", "prod", "staging", "dev", "monitoring"]
    for ns in k8s_namespaces:
        commands.append(_c("Kubernetes", f"Pods in {ns}", f"kubectl get pods -n {ns}", f"List pods in {ns}"))
        commands.append(_c("Kubernetes", f"Deployments in {ns}", f"kubectl get deploy -n {ns}", f"List deployments in {ns}"))
        commands.append(_c("Kubernetes", f"Services in {ns}", f"kubectl get svc -n {ns}", f"List services in {ns}"))

    # Security / audit quick commands
    security_commands = [
        ("Who am I", "whoami"),
        ("Current user groups", "id"),
        ("Sudo permissions", "sudo -l"),
        ("Last logins", "last -a | head -20"),
        ("Failed auth attempts", "sudo grep -i 'failed password' /var/log/auth.log | tail -50"),
        ("Recent sudo usage", "sudo grep -i 'sudo:' /var/log/auth.log | tail -50"),
        ("Open files by process", "lsof -p <pid> | head -50"),
        ("Open network files", "lsof -i -n -P | head -50"),
        ("Checksum SHA256", "sha256sum file.bin"),
        ("Verify checksum", "sha256sum -c checksums.txt"),
        ("Find world-writable", "find / -xdev -type d -perm -0002 2>/dev/null | head -100"),
        ("Find SUID files", "find / -xdev -type f -perm -4000 2>/dev/null | head -100"),
    ]
    for name, cmd in security_commands:
        commands.append(_c("Security", name, cmd, "Security/audit helper"))

    from app.cron_library import get_cron_command_library
    commands.extend(get_cron_command_library())

    # Keep deterministic and unique by (name, command)
    seen: set[tuple[str, str]] = set()
    unique: list[dict] = []
    for item in commands:
        key = (item["name"], item["command"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique

