#!/usr/bin/env bash
# Install NVIDIA driver on Dell/agentic-vm-os (A100 passthrough VM).
# Run on the GPU VM as a user with sudo:
#   bash scripts/install_nvidia_driver_dell.sh
# Then: sudo reboot
set -euo pipefail

echo "==> Detected NVIDIA PCI devices:"
lspci -nn | grep -i 'NVIDIA Corporation' || {
  echo "ERROR: brak NVIDIA w lspci — najpierw passthrough GPU do VM"
  exit 1
}

echo "==> Blacklist nouveau (jeśli aktywny)"
if lsmod | grep -q '^nouveau'; then
  echo "nouveau loaded — wyłączamy"
fi
echo 'blacklist nouveau' | sudo tee /etc/modprobe.d/blacklist-nouveau.conf >/dev/null
echo 'options nouveau modeset=0' | sudo tee -a /etc/modprobe.d/blacklist-nouveau.conf >/dev/null

echo "==> apt update + ubuntu-drivers"
sudo apt-get update -y
sudo apt-get install -y ubuntu-drivers-common

echo "==> Recommended drivers:"
ubuntu-drivers devices || true

# Prefer a recent open/proprietary branch available on Noble
DRIVER_PKG="${NVIDIA_DRIVER_PKG:-}"
if [[ -z "$DRIVER_PKG" ]]; then
  if apt-cache show nvidia-driver-570 >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-570
  elif apt-cache show nvidia-driver-565 >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-565
  elif apt-cache show nvidia-driver-550 >/dev/null 2>&1; then
    DRIVER_PKG=nvidia-driver-550
  else
    echo "ERROR: nie znaleziono pakietu nvidia-driver-*"
    exit 1
  fi
fi

echo "==> Installing $DRIVER_PKG (+ utils)"
UTILS_PKG="${DRIVER_PKG/nvidia-driver/nvidia-utils}"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$DRIVER_PKG" "$UTILS_PKG" || \
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$DRIVER_PKG"

# Multi-GPU A100 SXM often needs fabric manager (NVLink topology)
if apt-cache show nvidia-fabricmanager-550 >/dev/null 2>&1 || \
   apt-cache show "${DRIVER_PKG/nvidia-driver/nvidia-fabricmanager}" >/dev/null 2>&1; then
  FM="${DRIVER_PKG/nvidia-driver/nvidia-fabricmanager}"
  echo "==> Optional fabricmanager: $FM"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y "$FM" || true
fi

echo "==> update-initramfs"
sudo update-initramfs -u

echo
echo "==> DONE. Zrestartuj VM, potem:"
echo "    sudo reboot"
echo "Po reboot:"
echo "    nvidia-smi -L"
echo "    cd ~/NEW_TIZ_WEBSIDE- && source .venv/bin/activate && pip install vllm"
echo "    TP=\$(nvidia-smi -L | wc -l) bash scripts/run_vllm_a100.sh"
