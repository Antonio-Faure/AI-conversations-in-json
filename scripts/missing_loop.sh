#!/bin/bash
# Retente l'export des manquantes toutes les 30 min (throttling Perplexity).
# Stop quand les 347 fichiers sont la.
cd "$(dirname "$0")/.."
for i in $(seq 1 12); do
  n=$(find exports -path "*/perplexity/*.json" | wc -l)
  echo "=== TENTATIVE $i $(date +%H:%M) — $n/347 ==="
  if [ "$n" -ge 347 ]; then echo "COMPLET"; break; fi
  timeout -k 30 2400 .venv/bin/python scripts/export_missing.py perplexity || true
  sleep 1800
done
n=$(find exports -path "*/perplexity/*.json" | wc -l)
echo "=== FIN $(date +%H:%M) — $n/347 ==="
