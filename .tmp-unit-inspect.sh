#!/bin/bash
echo "=== systemd unit location ==="
sudo -n systemctl show np4m -p FragmentPath,DropInPaths,User,Environment,EnvironmentFile,ExecStart 2>/dev/null
echo
echo "=== unit file ==="
unit=$(sudo -n systemctl show np4m -p FragmentPath --value 2>/dev/null)
if [ -n "$unit" ]; then
  echo "--- $unit ---"
  sudo -n cat "$unit"
fi
echo
echo "=== drop-ins ==="
dropins=$(sudo -n systemctl show np4m -p DropInPaths --value 2>/dev/null)
for d in $dropins; do
  echo "--- $d ---"
  sudo -n cat "$d"
done
echo
echo "=== /home/np4m/ntnx-np4m HEAD ==="
sudo -n -u np4m git -C /home/np4m/ntnx-np4m log -1 --oneline 2>/dev/null
echo
echo "=== /home/np4m/ntnx-np4m existing files of interest ==="
sudo -n ls -la /home/np4m/ntnx-np4m/ | head -30
echo
echo "=== np4m's .np4m-probe.json ==="
sudo -n cat /home/np4m/ntnx-np4m/.np4m-probe.json 2>/dev/null || echo "(no probe file)"
echo "=== np4m's .np4m-master.json ==="
sudo -n cat /home/np4m/ntnx-np4m/.np4m-master.json 2>/dev/null || echo "(no master file)"
