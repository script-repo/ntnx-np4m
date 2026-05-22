#!/bin/bash
echo "=== all tcp listeners ==="
sudo -n ss -tlnp 2>/dev/null || ss -tlnp
echo
echo "=== all python processes ==="
ps -eo pid,ppid,etime,user,cmd | grep -E '(python|app\.py)' | grep -v grep
echo
echo "=== np4m-related files in homedir ==="
ls -la /home/rocky/ | grep -i np4m
echo
echo "=== np4m systemd units ==="
sudo -n systemctl list-units --no-pager --no-legend 2>/dev/null | grep -i np4m || echo "(none)"
echo
echo "=== content on :8443 (if present) ==="
timeout 3 curl -sk -o /tmp/8443.html -w "http %{http_code}\n" https://127.0.0.1:8443/ 2>/dev/null || echo "(no service)"
head -c 600 /tmp/8443.html 2>/dev/null
echo
echo "=== content on :8443 (http) ==="
timeout 3 curl -s -o /tmp/8443h.html -w "http %{http_code}\n" http://127.0.0.1:8443/ 2>/dev/null || echo "(no service)"
head -c 600 /tmp/8443h.html 2>/dev/null
