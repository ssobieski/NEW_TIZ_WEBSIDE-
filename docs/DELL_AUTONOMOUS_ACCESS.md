# Autonomiczny dostęp agenta do Della (VPN)

Cloud Agent **nie ma trasy do `200.0.0.110`** (sieć Cybertech VPN).
Sam sekret `DELL_SSH_PRIVATE_KEY` **nie wystarczy** z chmury — SSH i tak timeoutuje.
Działa: **self-hosted worker na Macu z włączonym VPN**.

## Opcja A (zalecana): Mac worker + VPN

Na MacBooku:

```bash
# 1) VPN Cybertech ON
# 2) klucz do Della
ls -la ~/dell_agent

# 3) worker w katalogu repo (sklonuj jeśli brak)
git clone https://github.com/ssobieski/NEW_TIZ_WEBSIDE-.git ~/NEW_TIZ_WEBSIDE-
cd ~/NEW_TIZ_WEBSIDE-
git checkout cursor/agent-monitoring-improve-2b9f

# 4) uruchom workera i ZOSTAW okno otwarte
cursor worker start
```

Potem w Cursor Cloud Agent:
- startuj **nowy** run z wyborem workera `mac-vpn-dell` (lub jak się nazywa),
- albo wklej link z `workerId=…` z dashboardu Agents.

Worker musi mieć dostęp do:
- SSH: `ssh -i ~/dell_agent ssobieski@200.0.0.110`
- opcjonalnie sekrety lokalne / env: `DELL_SSH`, `DELL_SSH_PRIVATE_KEY`

## Opcja B: sekret klucza (dla workera / fleet, nie samej chmury)

Dashboard → Cloud Agents → Environment  
https://cursor.com/dashboard/cloud-agents/environments/e/afb474ba-aa32-11f1-b532-320a589b8025

Secrets (My Secrets / Environment):

| Name | Value |
|------|--------|
| `DELL_SSH` | `ssobieski@200.0.0.110` |
| `DELL_SSH_PRIVATE_KEY` | cała zawartość `~/dell_agent` (PEM, z `-----BEGIN…`) |
| `VLLM_BASE_URL` | `http://127.0.0.1:8000` (gdy worker/SSH na Dellu) |

**Nie wklejaj klucza do chatu.** Tylko do Secrets.

Po dodaniu sekretu: **nowy** Cloud Agent run (stary run nie dostanie sekretu wstecz).

Na starcie agent powinien wykonać:
`bash scripts/setup_dell_ssh_from_secrets.sh`

## Opcja C: Ty na Dellu (bez workera)

```bash
cd ~/NEW_TIZ_WEBSIDE- && git pull && bash scripts/dell_tune_once.sh
```

Wklej raport z `----- BEGIN REPORT -----` z powrotem do agenta.

## Szybki test, że worker widzi Della

Na Macu (VPN ON):

```bash
ssh -i ~/dell_agent -o BatchMode=yes ssobieski@200.0.0.110 'hostname; curl -fsS --max-time 5 http://127.0.0.1:8000/v1/models | head -c 120; echo'
```

Jeśli to działa i `cursor worker start` jest aktywny — agent może iterować sam.
