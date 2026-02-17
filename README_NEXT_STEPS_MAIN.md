# README_NEXT_STEPS_MAIN

Objectif: lister les prochaines implémentations prioritaires **à partir de l'état actuel de `main`**.

## 1) Fiabiliser l'exécution "1st trade"
- Ajouter un mode explicite **one-shot** (1 seule tentative d'exec puis `trading_enabled=false`).
- Écrire l'arrêt automatique dans `data/control.json` avec raison (`source`, `reason`, `trade_id/run_id`).
- Envoyer un message Discord de confirmation quand le one-shot coupe le trading.

## 2) Vérifier la chaîne d'ordres CLOB de bout en bout
- Ajouter un check de pré-trade strict: balance/allowance/signer/API creds avant POST.
- En cas de `POST_FAILED`/`PARTIAL_POST_FAILED`, logger une cause normalisée (insufficient balance, auth, payload, timeout, etc.).
- Ajouter un compteur de succès réel (ordre ACK + fill) par fenêtre de temps.

## 3) Durcir la gestion des redémarrages automatiques
- Documenter et contrôler l'impact de `apt-daily-upgrade` sur `polymarket-bot.service`.
- Option: `systemctl edit polymarket-bot` pour comportement explicite pendant maintenance.
- Ajouter alerte ops "service restarted" avec timestamp + cause probable.

## 4) Observabilité / debugging
- Standardiser les `reason_code` côté Rust/Python (table unique dans doc).
- Créer une commande de diag rapide (script) qui affiche:
  - état `control.json`
  - dernier commit déployé
  - derniers `FAILED/CANCELLED`
  - uptime service
- Ajouter un export des erreurs critiques (fichier dédié + rotation).

## 5) Process de release
- Définir un flux clair `develop -> main`:
  - tag de test
  - fenêtre de validation
  - critères go/no-go
- Ajouter template PR orienté trading (checklist exécution réelle + logs).

## 6) Safety trading
- Kill-switch prioritaire et vérification à chaque tentative d'exec.
- Cooldown par market configurable et visible en logs.
- Guardrail de taille (max notional / max daily loss / max tries) activable par env.

## 7) Tests minimaux à ajouter
- Test unitaire: normalisation erreurs CLOB.
- Test intégration: passage OFF automatique après one-shot.
- Test de non-régression: pas de double submit sur même market pendant cooldown.

---

## Définition de "done" court terme
1. One-shot fonctionnel en prod.
2. Cause d'échec lisible immédiatement dans les logs/Discord.
3. Runbook de redémarrage + diagnostic rapide disponible.
