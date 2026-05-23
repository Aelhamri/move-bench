#!/usr/bin/env bash
# RQ2 - Setup script pour la VM Linux.
# A lancer une fois pour preparer l'environnement Flask + Playwright.

set -euo pipefail

SERVER_HOST="${SERVER_HOST:-developper@10.105.25.21}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/ssh_key}"
LOCAL_REPO="${LOCAL_REPO:-$HOME/RTDataHub}"
TUNNEL_PORT="${TUNNEL_PORT:-5433}"

echo "==> RTDataHub bench setup"
echo "    server     : $SERVER_HOST"
echo "    local repo : $LOCAL_REPO"
echo "    tunnel port: $TUNNEL_PORT"

# 1. Recuperer le code Flask + scripts bench depuis le serveur
mkdir -p "$LOCAL_REPO"
echo ""
echo "==> Synchro depuis le serveur (rsync)..."
rsync -av --delete \
    -e "ssh -i $SSH_KEY" \
    "$SERVER_HOST:/opt/RTDataHub/src/" \
    "$LOCAL_REPO/src/"
rsync -av \
    -e "ssh -i $SSH_KEY" \
    "$SERVER_HOST:/opt/RTDataHub/scripts/bench/" \
    "$LOCAL_REPO/scripts/bench/"
rsync -av \
    -e "ssh -i $SSH_KEY" \
    "$SERVER_HOST:/opt/RTDataHub/.env" \
    "$LOCAL_REPO/.env"
rsync -av \
    -e "ssh -i $SSH_KEY" \
    "$SERVER_HOST:/opt/RTDataHub/requirements.txt" \
    "$LOCAL_REPO/"

# 2. Installer les deps Python
echo ""
echo "==> Installation des dependances Python..."
cd "$LOCAL_REPO"
python3 -m pip install --user -r requirements.txt
python3 -m pip install --user 'psycopg[pool]' psutil playwright pymeos numpy scipy matplotlib

# 3. Browsers Playwright
echo ""
echo "==> Installation des navigateurs Playwright..."
python3 -m playwright install chromium

# 4. Adapter .env : pointer sur le tunnel local
echo ""
echo "==> Adaptation .env (port DB -> $TUNNEL_PORT)..."
sed -i.bak "s/^MOBILITYDB.PORT=.*/MOBILITYDB.PORT=$TUNNEL_PORT/" .env

# 5. Inject le frontend_bench.js dans index.html (si pas deja fait)
INDEX="$LOCAL_REPO/src/map/templates/index.html"
BENCH_JS="$LOCAL_REPO/scripts/bench/rq2/frontend_bench.js"
if ! grep -q "RQ2 - Frontend bench" "$INDEX"; then
    echo ""
    echo "==> Injection de frontend_bench.js dans index.html..."
    # Inject avant </body> via Python (plus robuste que sed pour HTML)
    python3 - <<EOF
from pathlib import Path
index_path = Path("$INDEX")
bench_js   = Path("$BENCH_JS").read_text()
content    = index_path.read_text()
needle     = "</body>"
inject     = "<script>\n// RQ2 - Frontend bench (auto-injected by setup_vm.sh)\n" + bench_js + "\n</script>\n"
if needle not in content:
    raise SystemExit("ERR: </body> not found in index.html")
content = content.replace(needle, inject + needle, 1)
index_path.write_text(content)
print(f"injected {len(bench_js)} bytes into {index_path}")
EOF
else
    echo "==> frontend_bench.js deja injecte, skip"
fi

# 6. Tunnel SSH
echo ""
echo "==> Verification tunnel SSH..."
if nc -z localhost "$TUNNEL_PORT" 2>/dev/null; then
    echo "    tunnel actif sur localhost:$TUNNEL_PORT"
else
    echo "    tunnel non actif. Lance le avec :"
    echo "    ssh -f -N -L $TUNNEL_PORT:localhost:5432 -i $SSH_KEY $SERVER_HOST"
    exit 1
fi

# 7. Test de connexion DB
echo ""
echo "==> Test connexion DB..."
PGPASSWORD=mobilitydb psql -h localhost -p "$TUNNEL_PORT" -U mobilitydb -d mobilitydb \
    -c "SELECT COUNT(*) FROM rt.stib_trip WHERE start_ts::date = '2026-04-24';" \
    || { echo "    ERR: connexion DB echouee"; exit 1; }

echo ""
echo "==> SETUP TERMINE"
echo ""
echo "Pour lancer le bench :"
echo "    cd $LOCAL_REPO"
echo "    python3 scripts/bench/rq2/orchestrator.py"
