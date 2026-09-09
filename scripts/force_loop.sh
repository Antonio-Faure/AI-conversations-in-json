#!/bin/bash
# Boucle de passes force : re-scrape/re-ecrit tout jusqu'au terminus.
# (passe suivante = reprise incrementielle en cas de kill/timeout)
for i in $(seq 1 8); do
  echo "=== FORCE PASS $i $(date +%H:%M:%S) ==="
  timeout -k 30 3300 .venv/bin/python run.py --all --force || true
done
echo "=== TERMINÉ $(date +%H:%M:%S)"
