#!/usr/bin/env bash
# Diagnoza GPU na Dell / agentic-vm-os (Cybertech VPN).
set -euo pipefail

echo "==> hostname: $(hostname)"
echo "==> kernel: $(uname -r)"
echo "==> lspci NVIDIA:"
lspci -nn | grep -iE 'nvidia|3d|vga' || echo "(brak wpisów NVIDIA w lspci — VM bez passthrough GPU?)"
echo
echo "==> nvidia modules:"
lsmod | grep -i nvidia || echo "(brak załadowanych modułów nvidia)"
echo
echo "==> nvidia-smi:"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi || echo "nvidia-smi FAILED — driver nie komunikuje się z GPU"
else
  echo "(nvidia-smi nie zainstalowany)"
  echo "  sudo apt update && sudo apt install -y nvidia-driver-550 nvidia-utils-550"
fi
echo
echo "==> /dev/nvidia*:"
ls -la /dev/nvidia* 2>/dev/null || echo "(brak device nodes — typowe gdy driver/GPU nie działa)"
echo
echo "==> virt hint:"
if [[ -r /sys/class/dmi/id/product_name ]]; then
  echo "product: $(cat /sys/class/dmi/id/product_name 2>/dev/null || true)"
fi
if command -v systemd-detect-virt >/dev/null 2>&1; then
  echo "virt: $(systemd-detect-virt || true)"
fi
echo
echo "Jeśli to VM (QEMU/KVM/VMware) i lspci nie pokazuje A100:"
echo "  → na hiperwizorze włącz GPU passthrough (4×A100) do tej VM i zrestartuj."
echo "Jeśli lspci widzi A100, a nvidia-smi nie:"
echo "  → zainstaluj/napraw driver i reboot."
