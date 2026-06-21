#!/bin/bash
# Sube cambios de CÓDIGO al repo sin tocar data/ ni docs/index.html, que el
# robot de GitHub Actions actualiza solo en cada corrida automática.
#
# Por qué existe: si corres `python scraper.py` en tu computador para probar
# y después haces `git add -A`, terminas subiendo una versión de los datos
# que choca con la que el robot ya subió — eso causa conflictos de merge.
# Este script solo agrega los archivos de código, nunca los datos.
#
# Uso: ./safe_push.sh "mensaje del commit"
set -e

MSG="${1:-Actualiza código}"

git add \
  scraper.py \
  weekly_summary.py \
  projects.json \
  requirements.txt \
  README.md \
  .gitignore \
  .github/workflows/*.yml

git status --short

git commit -m "$MSG"
git pull --rebase origin main
git push
