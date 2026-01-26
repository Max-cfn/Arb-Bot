# SETUP VM - Guide Installation UpCloud Amsterdam

> **Temps estimé** : 15-20 minutes
> **Prérequis** : Compte UpCloud, accès SSH

---

## 1️⃣ Créer/Migrer la VM vers Amsterdam

### Option A : Nouvelle VM

1. Connecte-toi à [UpCloud Console](https://hub.upcloud.com/)
2. **Deploy Server** → Sélectionne :
   - **Location** : `NL-AMS1` (Amsterdam, Netherlands)
   - **Plan** : `1 CPU, 1GB RAM` minimum (ou plus si budget)
   - **Storage** : 25GB SSD
   - **OS** : `Ubuntu Server 24.04 LTS`
3. **SSH Key** : Ajoute ta clé publique SSH
4. Note l'**IP publique** une fois créée

### Option B : Cloner ta VM Germany existante

1. Dans UpCloud Console → ta VM Germany
2. **Storage** → **Create backup**
3. **Deploy new server** → **From backup**
4. Sélectionne `NL-AMS1` comme location
5. Supprime l'ancienne VM Germany une fois testée

---

## 2️⃣ Connexion SSH

```bash
ssh root@<IP_VM_AMSTERDAM>
```

---

## 3️⃣ Script d'Installation (One-Liner)

Copie-colle ce bloc complet :

```bash
#!/bin/bash
set -e

echo "=========================================="
echo "🚀 Polymarket Arb Bot - Setup VM"
echo "=========================================="

# Update system
echo "[1/8] Updating system..."
apt update && apt upgrade -y

# Install dependencies
echo "[2/8] Installing dependencies..."
apt install -y \
    python3.12 \
    python3.12-venv \
    python3-pip \
    git \
    curl \
    wget \
    htop \
    tmux \
    sqlite3 \
    jq

# Create bot user (non-root)
echo "[3/8] Creating bot user..."
useradd -m -s /bin/bash botuser || true
mkdir -p /home/botuser/.ssh
cp ~/.ssh/authorized_keys /home/botuser/.ssh/ 2>/dev/null || true
chown -R botuser:botuser /home/botuser/.ssh
chmod 700 /home/botuser/.ssh

# Setup directory structure
echo "[4/8] Setting up directories..."
mkdir -p /opt/polymarket-bot
mkdir -p /opt/polymarket-bot/data
mkdir -p /opt/polymarket-bot/logs
chown -R botuser:botuser /opt/polymarket-bot

# Install Python packages globally needed
echo "[5/8] Setting up Python..."
python3.12 -m pip install --upgrade pip

# Configure firewall (allow SSH only)
echo "[6/8] Configuring firewall..."
apt install -y ufw
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable

# Set timezone UTC
echo "[7/8] Setting timezone to UTC..."
timedatectl set-timezone UTC

# Create swap (1GB) if not exists
echo "[8/8] Creating swap..."
if [ ! -f /swapfile ]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "=========================================="
echo "✅ Base setup complete!"
echo "=========================================="
echo ""
echo "Next steps:"
echo "1. Switch to botuser: su - botuser"
echo "2. Clone your repo"
echo "3. Setup .env file"
echo "4. Install Python dependencies"
echo ""
```

Exécute-le :
```bash
bash -c "$(cat <<'EOF'
<COLLE LE SCRIPT CI-DESSUS>
EOF
)"
```

Ou plus simple, crée un fichier :
```bash
nano setup_vm.sh
# Colle le script
chmod +x setup_vm.sh
./setup_vm.sh
```

---

## 4️⃣ Cloner le Repo (en tant que botuser)

```bash
# Passe en user botuser
su - botuser
cd /opt/polymarket-bot

# Clone ton repo (remplace par ton URL)
git clone https://github.com/TON_USERNAME/polymarket-arb-bot.git .

# OU si repo privé avec token
git clone https://TON_TOKEN@github.com/TON_USERNAME/polymarket-arb-bot.git .
```

---

## 5️⃣ Setup Python Environment

```bash
# Toujours en tant que botuser
cd /opt/polymarket-bot

# Créer virtual environment
python3.12 -m venv venv
source venv/bin/activate

# Installer dépendances
pip install -r requirements.txt
```

---

## 6️⃣ Configuration (.env)

```bash
# Copier le template
cp .env.example .env

# Éditer avec tes valeurs
nano .env
```

**Remplis ces valeurs :**

```env
# Discord Webhooks (copie depuis Discord)
DISCORD_WEBHOOK_HEALTH=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_OPS=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_DAILY=https://discord.com/api/webhooks/xxx/yyy
DISCORD_WEBHOOK_OPPORTUNITIES=https://discord.com/api/webhooks/xxx/yyy

# Detection params (garde les défauts pour commencer)
MIN_EDGE_PERCENT=0.5
MIN_LIQUIDITY_USD=100
MAX_MARKETS_WATCH=500
```

---

## 7️⃣ Test Geoblock

**IMPORTANT** : Vérifie que ton IP n'est pas bloquée AVANT de continuer.

```bash
# Active l'environnement
source venv/bin/activate

# Test rapide
python scripts/check_geoblock.py
```

**Résultat attendu :**
```
✅ IP not blocked
   IP: 185.xxx.xxx.xxx
   Country: NL
   Region: North Holland
```

**Si bloqué :**
```
❌ IP is BLOCKED
   IP: xxx.xxx.xxx.xxx
   Country: FR
```
→ Problème avec ta VM, vérifie qu'elle est bien à Amsterdam.

---

## 8️⃣ Test Discord Webhooks

```bash
python scripts/test_discord.py
```

Tu devrais voir des messages arriver dans tes channels Discord.

---

## 9️⃣ Premier Lancement (Test)

```bash
# Lancement manuel pour test
python -m src.main

# Ou avec logs visibles
python -m src.main 2>&1 | tee logs/startup.log
```

Vérifie :
- Message "🟢 Bot starting" dans Discord #health
- Pas d'erreurs dans la console
- `Ctrl+C` pour arrêter

---

## 🔟 Setup Service Systemd (Production)

```bash
# Retour en root
exit

# Copier le service file
cp /opt/polymarket-bot/systemd/polymarket-bot.service /etc/systemd/system/

# Recharger systemd
systemctl daemon-reload

# Activer au démarrage
systemctl enable polymarket-bot

# Démarrer
systemctl start polymarket-bot

# Vérifier status
systemctl status polymarket-bot
```

---

## 📊 Commandes Utiles

```bash
# Voir les logs en temps réel
journalctl -u polymarket-bot -f

# Voir les 100 dernières lignes
journalctl -u polymarket-bot -n 100

# Redémarrer le bot
systemctl restart polymarket-bot

# Arrêter le bot
systemctl stop polymarket-bot

# Vérifier si le process tourne
ps aux | grep python

# Voir l'utilisation ressources
htop
```

---

## 🔄 Mise à Jour du Code

```bash
# En tant que botuser
su - botuser
cd /opt/polymarket-bot

# Pull les derniers changements
git pull origin main

# Mettre à jour les dépendances si nécessaire
source venv/bin/activate
pip install -r requirements.txt

# Redémarrer le service (en root)
exit
systemctl restart polymarket-bot
```

---

## 🛡️ Sécurité Additionnelle (Optionnel)

### Fail2ban (protection brute-force SSH)

```bash
apt install fail2ban -y
systemctl enable fail2ban
systemctl start fail2ban
```

### Désactiver login root SSH (après avoir testé botuser)

```bash
# Édite sshd_config
nano /etc/ssh/sshd_config

# Change :
PermitRootLogin no

# Redémarre SSH
systemctl restart sshd
```

### Automatic Security Updates

```bash
apt install unattended-upgrades -y
dpkg-reconfigure -plow unattended-upgrades
```

---

## ❓ Troubleshooting

### "Permission denied" sur git clone
```bash
# Assure-toi d'être botuser
whoami  # devrait afficher "botuser"

# Si repo privé, utilise un Personal Access Token
git clone https://TOKEN@github.com/user/repo.git
```

### "Module not found" au lancement
```bash
# Vérifie que le venv est activé
which python  # devrait afficher /opt/polymarket-bot/venv/bin/python

# Si non :
source /opt/polymarket-bot/venv/bin/activate
```

### Bot crash immédiatement
```bash
# Vérifie les logs
journalctl -u polymarket-bot -n 50

# Causes communes :
# - .env manquant ou mal formaté
# - Webhooks Discord invalides
# - IP géobloquée
```

### WebSocket se déconnecte souvent
- Normal si connexion instable
- Le bot reconnecte automatiquement
- Si > 5 reconnexions/heure → check réseau VM

---

## ✅ Checklist Finale

- [ ] VM créée à Amsterdam (NL-AMS1)
- [ ] Script setup exécuté
- [ ] Repo cloné
- [ ] .env configuré avec webhooks Discord
- [ ] Test geoblock passé (IP non bloquée)
- [ ] Test Discord passé (messages reçus)
- [ ] Service systemd configuré
- [ ] Bot tourne et envoie health checks

---

**🎉 Ta VM est prête ! Le bot surveille maintenant Polymarket 24/7.**
