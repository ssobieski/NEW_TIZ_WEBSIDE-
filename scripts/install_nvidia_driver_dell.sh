#!/usr/bin/env bash
# Install NVIDIA driver on Dell/agentic-vm-os (A100 passthrough VM).
# Prefer a single consistent driver stack — do NOT mix -570 meta with -580
# fabricmanager (that removes the driver packages).
#
#   bash scripts/install_nvidia_driver_dell.sh
#   sudo reboot
set -euo pipefail

echo "==> Detected NVIDIA PCI devices:"
lspci -nn | grep -i 'NVIDIA Corporation' || {
  echo "ERROR: brak NVIDIA w lspci — najpierw passthrough GPU do VM"
  exit 1
}

echo "==> Blacklist nouveau"
echo 'blacklist nouveau' | sudo tee /etc/modprobe.d/blacklist-nouveau.conf >/dev/null
echo 'options nouveau modeset=0' | sudo tee -a /etc/modprobe.d/blacklist-nouveau.conf >/dev/null

sudo apt-get update -y
sudo apt-get install -y ubuntu-drivers-common dkms

echo "==> Recommended drivers:"
ubuntu-drivers devices || true

# Prefer open/recommended stack for A100 on Noble; allow override.
DRIVER_PKG="${NVIDIA_DRIVER_PKG:-}"
if [[ -z "$DRIVER_PKG" ]]; then
  if apt-cache show nvidia-driver-595-open >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-595-open
  elif apt-cache show nvidia-driver-580-open >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-580-open
  elif apt-cache show nvidia-driver-580 >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-580
  else
    echo "ERROR: nie znaleziono nvidia-driver-*"
    exit 1
  fi
fi

# Derive branch number: nvidia-driver-595-open → 595
BRANCH="$(echo "$DRIVER_PKG" | sed -E 's/nvidia-driver-([0-9]+).*/\1/')"
UTILS_PKG="nvidia-utils-${BRANCH}"

echo "==> Installing consistent stack: $DRIVER_PKG + $UTILS_PKG"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$DRIVER_PKG" "$UTILS_PKG"

# Fabric manager ONLY if matching package exists AND won't purge the driver.
# Skip by default (INSTALL_FABRICMANAGER=1 to enable).
if [[ "${INSTALL_FABRICMANAGER:-0}" == "1" ]]; then
  FM="nvidia-fabricmanager-${BRANCH}"
  if apt-cache show "$FM" >/dev/null 2>&1; then
    echo "==> Installing $FM"
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$FM" || true
  else
    echo "WARN: brak $FM — pomijam"
  fi
else
  echo "==> Pomijam fabricmanager (ustaw INSTALL_FABRICMANAGER=1 jeśli NVLink wymaga)."
fi

sudo update-initramfs -u

echo
echo "==> DONE. Zrestartuj VM:"
echo "    sudo reboot"
echo "Po reboot (SSH ponownie na 200.0.0.110):"
echo "    nvidia-smi -L"
echo "    cd ~/NEW_TIZ_WEBSIDE- && source .venv/bin/activate && pip install vllm"
echo "    bash scripts/run_vllm_a100.sh"
