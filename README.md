# Nexaz Code Cloud (MVP)

Un service web minimal qui transforme une **tâche en langage naturel** en
**script exécuté dans un bac à sable isolé**, avec le code généré, la sortie
standard, la sortie d'erreur, le code de sortie et la durée.

> **Ce n'est pas Claude Code Cloud.** C'est un MVP honnête et limité :
> un seul fichier exécuté, pas de conteneur par utilisateur, pas de clone de
> dépôt, pas d'accès aux fichiers de l'utilisateur. Voir « Limites » plus bas.

## API

| Méthode | Route | Description |
|---|---|---|
| `POST` | `/api/task` | `{"task": "…", "lang": "python"\|"bash", "timeout_s": 10, "asynchrone": false}` → génère, exécute, renvoie le résultat complet |
| `GET` | `/api/task/{id}` | État + résultat d'une tâche (polling) |
| `GET` | `/api/tasks` | Les dernières tâches de l'instance |
| `GET` | `/sante` | `{"ok": true, …}` + joignabilité du modèle |
| `GET` | `/` | Interface web (HTML inline) |

Réponse `200` (succès) :

```json
{
  "tache_id": "417c1730708a",
  "refuse": false,
  "code_genere": "for i in range(1, 6):\n    print(i)",
  "stdout": "1\n2\n3\n4\n5\n",
  "stderr": "",
  "exit_code": 0,
  "duree_s": 3.78,
  "duree_modele_s": 3.71,
  "duree_execution_s": 0.06,
  "timeout_atteint": false,
  "sortie_tronquee": false,
  "modele": "nexaz-core",
  "statut": "terminee"
}
```

Réponse `403` (refus par la liste noire) :

```json
{
  "refuse": true,
  "raison": "Tâche refusée par la liste noire : chemin système absolu",
  "regle": "chemin système absolu",
  "motif": "/etc",
  "etape_refus": "tache",
  "code_genere": null,
  "exit_code": null,
  "statut": "refusee"
}
```

Codes d'erreur : `400` entrée invalide · `403` refus de sécurité ·
`429` trop de tâches · `503` modèle local injoignable.

## Architecture

```
POST /api/task
   │
   ├─ 1. FILTRE liste noire sur la TÂCHE          (sandbox.verifier_securite)
   ├─ 2. MODÈLE  : Nexaz Core (llama.cpp, CPU) écrit le script   (nexaz_client)
   ├─ 3. FILTRE liste noire sur le CODE généré    (les 2 doivent passer)
   └─ 4. EXÉCUTION dans un processus enfant jetable (sandbox.executer_code)
```

Fichiers :

- `app.py` — routes FastAPI + page web (HTML/CSS/JS inline, thème sombre)
- `sandbox.py` — liste noire, répertoire jetable, timeout dur, `rlimits`,
  environnement nettoyé, troncature des sorties
- `nexaz_client.py` — client HTTP du modèle local `nexaz-core`
- `vercel.json` — `maxDuration: 60` pour `app.py`

## Mitigations de sécurité réellement implémentées

1. **Filtre à deux étages** — la liste noire est appliquée *avant* l'appel au
   modèle (sur le texte de la tâche) **et** *après* (sur le code généré).
   Un modèle qui « obéit » à une demande dangereuse ne passe pas au travers.
2. **Répertoire de travail jetable** — `tempfile.mkdtemp(prefix="nexaz-tache-")`,
   supprimé dans un `finally` (même en cas de timeout ou d'exception).
3. **Timeout dur** — 10 s par défaut (borné 1–20 s). À l'expiration :
   `killpg(SIGKILL)` sur tout le groupe de processus (le processus est lancé
   avec `setsid()` pour qu'il forme son propre groupe).
4. **Liste noire** (~40 motifs) : `rm -rf`, `rmdir`, `sudo`, `su`, `doas`,
   fork bomb, `dd if=`, `mkfs`, `shutdown`/`reboot`, `mount`, `crontab`,
   `chmod`/`chown`, `kill`/`pkill`, `curl`/`wget`/`nc`/`ssh`/`scp`/`socat`,
   `eval`/`exec`, `__import__`, `os.system`/`os.popen`, `subprocess`,
   `shutil.rmtree`, `ctypes`, `os.fork`, `multiprocessing`, `import socket`,
   `requests`, `urllib`, `pip install`, chemins absolus (`/etc`, `/proc`,
   `/sys`, `/dev`, `/root`, `/home`, `/var`, `/usr`, `/tmp`…), `../`,
   `~/`, `.ssh`.
5. **Aucun accès aux fichiers du serveur** — tout littéral de chemin absolu
   (entre quotes ou en argument) et toute remontée `../` sont refusés ;
   le `cwd` est le répertoire jetable et `HOME` y est redirigé (donc `~` ne
   pointe pas vers le vrai dossier personnel).
6. **Environnement nettoyé** — `os.environ` n'est **jamais** transmis :
   l'enfant ne voit que 11 variables construites à la main (`PATH`, `HOME`,
   `TMPDIR`, `LANG`…). Aucun jeton GitHub/Vercel/Discord/clé LLM ne fuit.
7. **Limites noyau (`rlimit`)** — CPU (`timeout+2 s`), taille de fichier
   écrite (1 Mo), mémoire (512 Mo), descripteurs de fichiers (64).
8. **Sorties tronquées** — 6 000 octets par flux, avec l'indicateur
   `sortie_tronquee` ; `stdin` est branché sur `/dev/null` (pas de blocage
   sur `input()`).
9. **Limiteur de débit** — 12 tâches / 5 min / IP (HTTP 429).
10. **Validation d'entrée** — tâche ≤ 800 caractères, code ≤ 20 Ko,
    langage ∈ {python, bash}, timeout borné.

## Limites — ce qui n'est PAS fait (honnêtement)

- **Pas de conteneur par utilisateur, pas de VM, pas de namespace.**
  Techniquement impossible sur cette infrastructure (2 Go de RAM déjà
  occupés par le serveur du modèle). Un script partage le noyau et
  l'utilisateur système de l'hôte.
- **La liste noire est un filtre textuel, pas une barrière noyau.** Un
  attaquant déterminé peut la contourner (encodage, `chr()`, base64…).
  Un enfant qui appelle `setsid()` peut survivre au `killpg`.
  Le réseau n'est pas coupé au niveau noyau : on refuse les motifs connus.
  Pas de seccomp, pas d'AppArmor, pas de user namespace.
  → Le module vise **« du code écrit par un LLM qui se trompe »**, pas
  « du code écrit par un adversaire ». Pour ce dernier cas il faudrait
  gVisor / Firecracker / un vrai conteneur par tâche.
- **Pas de multi-session, pas d'authentification, pas de comptes.**
  Pas de conversation continue : chaque tâche est indépendante.
- **Aucun accès aux fichiers de l'utilisateur, ni clone de dépôt, ni
  écriture de PR.** La seule entrée est une chaîne de texte.
- **Le stockage des tâches est en mémoire** : sur un hébergement
  serverless, `GET /api/task/{id}` ne fonctionne que dans l'instance qui a
  traité la tâche (donc pendant quelques minutes). Pas de base de données.
- **Modèle Qwen2.5-3B en CPU (~11 tokens/s, contexte 1024 tokens)** : le
  code produit est simple et parfois imparfait. Un script complexe peut être
  tronqué (256 tokens de sortie).
- **Pas de langage hors Python/Bash.**
- Le modèle est servi sur le poste et exposé par un **tunnel Cloudflare
  éphémère** : si l'URL du tunnel change, la variable `NEXAZ_CORE_URL` du
  projet Vercel doit être remise à jour (sinon `/sante` renvoie
  `modele_joignable: false` et `POST /api/task` répond 503 — jamais de
  résultat inventé).
- **Plafond de 60 s** par requête (limite Vercel) : le temps est réparti
  entre le modèle (~3–25 s) et l'exécution (≤ 10 s).

## Développement local

```bash
pip install -r requirements.txt
NEXAZ_CORE_URL=http://127.0.0.1:8100 python3 -m uvicorn app:app --port 8200
curl -s -X POST localhost:8200/api/task -H 'Content-Type: application/json' \
  -d '{"task":"affiche les nombres de 1 a 5","lang":"python"}'
```

## Déploiement

```bash
export PATH="/home/furax/nodejs/bin:$PATH"
vercel link --yes --project nexaz-code-cloud
vercel env add NEXAZ_CORE_URL production --value "<url-du-tunnel>" --force --yes
vercel deploy --yes --prod
vercel alias set <deploiement> nexaz-code-cloud.vercel.app
```

⚠️ `vercel --prod` crée une **nouvelle URL** : sans le `vercel alias set`,
l'alias stable continue de servir l'ancienne build.
