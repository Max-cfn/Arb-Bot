# README_NEXT_STEPS_DEVELOP

Contexte: sur `develop`, **les ordres ne passent pas de façon fiable**. Ce document liste les actions prioritaires pour corriger ça.

## Symptômes observés
- Échecs fréquents `FAILED` / `CANCELLED` sur la phase d'exécution.
- Cas typiques: `POST_FAILED`, `PARTIAL_POST_FAILED`, `TIMEOUT_NO_FILLS`.
- Incertitude sur la cause exacte sans lecture fine des logs.

## Priorité P0 (bloquants exécution)
1. **Instrumenter précisément les erreurs POST CLOB**
   - Capturer code HTTP, payload rejeté (redacté), endpoint, et mapping vers `reason_code` stable.
   - Différencier explicitement: auth invalide vs balance/allowance vs payload invalide vs timeout réseau.

2. **Pré-check avant envoi d'ordres**
   - Vérifier balance USDC dispo + allowance + creds API + signer.
   - Refuser l'exécution en amont avec `reason_code=PRECHECK_FAILED` détaillé.

3. **Validation du payload ordre (schema/runtime)**
   - Ajouter validation locale stricte avant POST.
   - Logger un diff minimal entre payload attendu et payload final signé.

## Priorité P1 (stabilité)
4. **Retry policy contrôlée**
   - Retry borné et conditionnel (pas de retry aveugle sur erreurs logiques 4xx).
   - Backoff court + jitter sur erreurs réseau uniquement.

5. **Gestion partielle robuste**
   - Si un leg passe et l'autre rate: procédure unique de recovery/unwind documentée.
   - Éviter les états intermédiaires silencieux.

6. **Protection anti-double exécution**
   - Vérifier que le cooldown par market est bien actif et traçable en logs.
   - Ajouter métrique "execution skipped by cooldown".

## Priorité P2 (ops / runbook)
7. **Runbook incident “orders not working”**
   - Commandes de diagnostic en <2 minutes.
   - Arbre de décision par `reason_code`.

8. **Alerte Discord orientée action**
   - Message compact avec cause, run_id, market_id, et action recommandée.

## Tests à ajouter sur develop
- Test d'intégration: pre-check échoue proprement quand allowance insuffisante.
- Test d'intégration: payload invalide => `PRECHECK_FAILED` (pas de POST).
- Test d'intégration: timeout => retry borné, puis arrêt propre.

---

## Définition de done pour develop
- On peut expliquer chaque échec en 1 ligne (cause claire).
- Les faux `TIMEOUT_NO_FILLS` diminuent.
- Les ordres valides passent de manière reproductible en fenêtre de test.
