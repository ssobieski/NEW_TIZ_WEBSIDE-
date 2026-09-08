#!/usr/bin/env bash
# Jeśli lokalny config/industry.yaml ma 0 RSS lub 0 media — skopiuj pełny profil TIZ.
# Bez tego agentic run widzi tylko firm_discovery (puste „Źródła mediów”).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
EXAMPLE="config/tiz_cutting_tools.example.yaml"
TARGET="config/industry.yaml"

python_count() {
  python - <<'PY'
from market_agents.config import load_config
c = load_config("config/industry.yaml")
media = len(getattr(c.sources, "media", None) or [])
social = len(getattr(c.sources, "social", None) or [])
lit = len(getattr(c.sources, "literature", None) or [])
print(len(c.sources.rss), len(c.sources.web), media, social, lit)
PY
}

if [[ ! -f "$EXAMPLE" ]]; then
  echo "Brak $EXAMPLE"
  exit 1
fi

apply_local_vllm() {
  python - <<'PY'
from pathlib import Path
import re
p = Path("config/industry.yaml")
t = p.read_text(encoding="utf-8")
t = re.sub(r'(base_url:\s*).*', r'\1"http://127.0.0.1:8000"', t, count=1)
t = re.sub(r'(provider:\s*).*', r'\1vllm', t, count=1)
p.write_text(t, encoding="utf-8")
PY
}

if [[ ! -f "$TARGET" ]]; then
  cp "$EXAMPLE" "$TARGET"
  apply_local_vllm
  echo "==> Utworzono $TARGET z przykładu TIZ"
else
  read -r RSS WEB MEDIA SOCIAL LIT <<<"$(python_count 2>/dev/null || echo "0 0 0 0 0")"
  echo "==> Obecne źródła: rss=$RSS web=$WEB media=$MEDIA social=$SOCIAL literature=$LIT"
  if [[ "${RSS:-0}" -lt 1 || "${MEDIA:-0}" -lt 1 ]]; then
    cp "$EXAMPLE" "$TARGET"
    apply_local_vllm
    echo "==> Skopiowano pełne źródła TIZ (rss lub media było 0)"
  else
    echo "==> OK — nie nadpisuję (RSS>0 i media>0)"
  fi
fi

echo -n "==> Po ensure: rss web media social literature = "
python_count
