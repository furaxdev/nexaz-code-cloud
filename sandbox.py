# -*- coding: utf-8 -*-
"""
sandbox.py — Bac à sable d'exécution de Nexaz Code Cloud.

C'est le cœur du produit : on exécute du code écrit par un LLM dans un
processus enfant isolé, avec une liste noire de motifs dangereux, un
répertoire de travail temporaire propre à chaque tâche, un timeout dur
et un environnement (variables d'env) nettoyé.

Honnêteté : ce N'EST PAS un conteneur, ni une VM, ni une jail. C'est un
processus enfant avec des limites de ressources. Le module est écrit pour
résister aux erreurs de bonne foi (script qui boucle, script qui écrit
partout dans son dossier), PAS à un attaquant déterminé qui connaîtrait
les angles morts (voir LIMITES en bas de fichier).
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:  # POSIX uniquement — sur une plateforme sans `resource`, on dégrade.
    import resource as _resource
except ImportError:  # pragma: no cover
    _resource = None


# ════════════════════════════════════════════════════════════════════════════
# 1. Constantes
# ════════════════════════════════════════════════════════════════════════════

TIMEOUT_DEFAUT = 10.0          # secondes — timeout dur par tâche
TIMEOUT_MIN = 1.0
TIMEOUT_MAX = 20.0
MAX_SORTIE_OCTETS = 6000       # par flux (stdout / stderr), tronqué au-delà
MAX_CODE_OCTETS = 20_000       # taille max du code accepté
MAX_FICHIER_OCTETS = 1_000_000 # RLIMIT_FSIZE : 1 Mo écrit max par le process
MAX_MEMOIRE_OCTETS = 512 * 1024 * 1024  # RLIMIT_AS : 512 Mo

LANGAGES = {"python": ".py", "bash": ".sh"}

EXTENSIONS = {".py": "python", ".sh": "bash"}


class RefusSecurite(Exception):
    """Levée quand le code (ou la tâche) heurte la liste noire."""

    def __init__(self, regle: str, motif: str, etape: str):
        self.regle = regle          # description lisible de ce qui est interdit
        self.motif = motif          # l'extrait exact qui a déclenché le refus
        self.etape = etape          # « tache » ou « code »
        super().__init__(f"Refusé ({regle})")

    def as_dict(self) -> dict:
        return {
            "regle": self.regle,
            "motif": self.motif,
            "etape": self.etape,
        }


# ════════════════════════════════════════════════════════════════════════════
# 2. Liste noire — patterns vérifiés AVANT toute exécution
# ════════════════════════════════════════════════════════════════════════════

# (regex, description). L'ordre compte : la première règle qui matche gagne.
REGLES_INTERDITES: list[tuple[str, str]] = [
    # ---- destruction de fichiers -------------------------------------------
    (r"\brm\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*[rf]", "suppression récursive/forcée (rm -rf)"),
    (r"\brm\s+-\S", "suppression forcée (rm -f)"),
    (r"\brmdir\b", "suppression de répertoire (rmdir)"),
    (r"\bshred\b", "destruction de fichier (shred)"),
    (r"\bfind\b[^\n]*\s-delete\b", "suppression via find -delete"),

    # ---- escalade de privilèges -------------------------------------------
    (r"\bsudo\b", "escalade de privilèges (sudo)"),
    (r"\bdoas\b", "escalade de privilèges (doas)"),
    (r"(?:^|[\s;&|`(])su\s", "changement d'utilisateur (su)"),
    (r"\bpkexec\b", "escalade de privilèges (pkexec)"),

    # ---- écriture / lecture hors du dossier de travail ---------------------
    (r"/(?:etc|proc|sys|dev|root|boot|var|usr|bin|sbin|lib|lib64|opt|srv|"
     r"home|run|mnt|media|tmp)\b", "chemin système absolu"),
    (r"""["']/[A-Za-z0-9_.~]""", "chemin absolu après une quote"),
    (r"(?:^|[\s;|&(=<>])/(?![>\s])", "chemin absolu en argument"),
    (r"\.\./", "remontée de répertoire (../)"),
    (r"(?:^|[\s\"'=])~/", "accès au dossier personnel (~/)"),
    (r"\.ssh\b", "accès aux clés SSH (.ssh)"),
    (r"\.aws\b|\.gnupg\b|\.kube\b|\.docker\b|\.git-credentials\b",
     "accès à des identifiants système"),
    (r"\bcat\s+/(?!dev/null)", "lecture d'un fichier système (cat /)"),
    (r"/etc/passwd|/etc/shadow|/etc/hosts|/proc/", "fichier système sensible"),

    # ---- bombes / déni de service -----------------------------------------
    (r":\s*\(\s*\)\s*\{", "fork bomb"),
    (r"\bos\.fork\b|os\.spawn|multiprocessing|pty\.spawn", "création de processus"),
    (r"\b(?:kill|pkill|killall)\b", "envoi de signaux aux processus"),
    (r"\bdd\s+if=", "écriture disque brute (dd)"),
    (r"\bmkfs(?:\.[a-z0-9]+)?\b|\bformat\s+[a-zA-Z]:|\bfdisk\b", "formatage de disque"),
    (r"\b(?:shutdown|reboot|halt|poweroff|init\s+0)\b", "arrêt de la machine"),
    (r"\b(?:mount|umount|insmod|modprobe|sysctl)\b", "manipulation du noyau"),

    # ---- évasion / exécution dynamique ------------------------------------
    (r"\beval\s*\(", "exécution dynamique (eval)"),
    (r"\bexec\s*\(", "exécution dynamique (exec)"),
    (r"\b__import__\b", "import dynamique (__import__)"),
    (r"\bos\.system\b|\bos\.popen\b|\bos\.exec", "exécution de commande système (os.system)"),
    (r"\bsubprocess\b", "exécution de commande système (subprocess)"),
    (r"shutil\.rmtree", "suppression récursive (shutil.rmtree)"),
    (r"\bctypes\b", "accès bas niveau (ctypes)"),
    (r"\b(?:os\.kill|os\.killpg|os\.setuid|os\.setgid)\b", "manipulation de processus"),
    (r"\bchmod\b|\bchown\b|\bchgrp\b", "changement de permissions/propriétaire"),
    (r"\bcrontab\b|\bsystemctl\b|\bservice\s+\w+\s+(?:start|stop)", "persistance système"),

    # ---- réseau (exfiltration / rebond) -----------------------------------
    (r"\b(?:curl|wget|nc|ncat|netcat|telnet|socat|ftp|ssh|scp|rsync)\b",
     "accès réseau sortant"),
    (r"\bimport\s+(?:socket|requests|urllib|http|ftplib|paramiko|telnetlib|smtplib)\b"
     r"|\bfrom\s+(?:socket|requests|urllib|http|ftplib|paramiko|telnetlib|smtplib)\s+import",
     "accès réseau en Python"),
    (r"\bsocket\.|\burlopen\s*\(|requests\.(?:get|post)", "accès réseau"),
    (r"\b(?:pip|pip3|conda|apt|apt-get|npm|yarn|gem|cargo|go)\s+(?:install|get|add)\b",
     "installation de paquets"),
]

_REGLES_COMPILEES = [
    (re.compile(motif, re.IGNORECASE | re.MULTILINE), desc)
    for motif, desc in REGLES_INTERDITES
]


def verifier_securite(texte: str, etape: str = "code") -> None:
    """Analyse `texte` et lève RefusSecurite au premier motif dangereux.

    `etape` vaut « tache » (texte demandé par l'utilisateur) ou « code »
    (code généré). On applique exactement les mêmes règles aux deux : on ne
    demande pas au modèle d'écrire quelque chose qu'on refuserait ensuite,
    et inversement un modèle qui « obéit » ne peut pas passer au travers.
    """
    if not isinstance(texte, str) or not texte.strip():
        raise RefusSecurite("entrée vide", "", etape)

    # Les lignes `#!...` (shebang) sont ignorées : on n'exécute JAMAIS le
    # fichier directement, on passe toujours par `python <fichier>` ou
    # `bash <fichier>` — le shebang y est un simple commentaire. Sans ça,
    # un `#!/usr/bin/env python3` légitime serait refusé par la règle
    # « chemin système absolu ».
    analyse = re.sub(r"(?m)^\s*#!.*$", "", texte)

    for regex, desc in _REGLES_COMPILEES:
        m = regex.search(analyse)
        if m:
            extrait = m.group(0).strip()
            if len(extrait) > 60:
                extrait = extrait[:57] + "…"
            raise RefusSecurite(desc, extrait, etape)


# ════════════════════════════════════════════════════════════════════════════
# 3. Environnement nettoyé — aucune fuite de secret vers le code exécuté
# ════════════════════════════════════════════════════════════════════════════

def environnement_propre(dossier: str) -> dict:
    """Retourne un env MINIMAL, construit à la main.

    On ne copie RIEN de os.environ : le processus enfant ne voit donc aucun
    jeton (GitHub, Vercel, Discord, clés LLM…) présent dans le conteneur.
    """
    bin_dir = os.path.dirname(sys.executable or "/usr/bin/python3")
    chemin = os.pathsep.join([bin_dir, "/usr/local/bin", "/usr/bin", "/bin"])
    return {
        "PATH": chemin,
        "HOME": dossier,          # `~` = le dossier de travail, pas le vrai HOME
        "TMPDIR": dossier,
        "PWD": dossier,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "TZ": "UTC",
        "TERM": "dumb",
    }


# ════════════════════════════════════════════════════════════════════════════
# 4. Limites de ressources du processus enfant
# ════════════════════════════════════════════════════════════════════════════

def _preexec(timeout: float):
    """Prépare le processus enfant : nouveau groupe de processus + rlimits."""
    def _appliquer():
        # Nouveau groupe de session : permet de tuer TOUT l'arbre avec killpg.
        try:
            os.setsid()
        except OSError:
            pass
        if _resource is None:
            return
        cpu = int(timeout) + 2
        limites = [
            (_resource.RLIMIT_CPU, (cpu, cpu + 2)),
            (_resource.RLIMIT_FSIZE, (MAX_FICHIER_OCTETS, MAX_FICHIER_OCTETS)),
            (_resource.RLIMIT_NOFILE, (64, 64)),
            (_resource.RLIMIT_AS, (MAX_MEMOIRE_OCTETS, MAX_MEMOIRE_OCTETS)),
        ]
        for quoi, val in limites:
            try:
                _resource.setrlimit(quoi, val)
            except (ValueError, OSError):
                pass
    return _appliquer


# ════════════════════════════════════════════════════════════════════════════
# 5. Exécution
# ════════════════════════════════════════════════════════════════════════════

def _tronquer(blob: bytes, limite: int = MAX_SORTIE_OCTETS) -> tuple[str, bool]:
    """Décode + tronque proprement. Retourne (texte, tronqué ?)."""
    tronque = len(blob) > limite
    if tronque:
        blob = blob[:limite]
    texte = blob.decode("utf-8", errors="replace")
    return texte, tronque


def _lire_fichier(chemin: Path) -> tuple[str, bool]:
    try:
        with open(chemin, "rb") as f:
            return _tronquer(f.read(MAX_SORTIE_OCTETS + 1))
    except FileNotFoundError:
        return "", False


def executer_code(code: str, lang: str = "python",
                  timeout: float = TIMEOUT_DEFAUT) -> dict:
    """Exécute `code` dans un bac à sable jetable.

    Retourne un dict : {stdout, stderr, exit_code, duree_s, timeout_atteint,
    sortie_tronquee, refus}. Ne lève jamais pour une erreur du code exécuté.
    """
    if lang not in LANGAGES:
        raise ValueError(f"Langage non supporté : {lang!r} (attendu : python, bash)")
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Code vide")
    if len(code) > MAX_CODE_OCTETS:
        raise ValueError(f"Code trop long ({len(code)} > {MAX_CODE_OCTETS} octets)")

    timeout = max(TIMEOUT_MIN, min(float(timeout), TIMEOUT_MAX))

    # ---- FILTRE 1 : refus avant même de créer un processus -----------------
    verifier_securite(code, etape="code")

    dossier = tempfile.mkdtemp(prefix="nexaz-tache-")
    debut = time.monotonic()
    proc = None
    try:
        script = Path(dossier) / f"script{LANGAGES[lang]}"
        script.write_text(code, encoding="utf-8")

        if lang == "python":
            # -I : mode isolé (ignore PYTHONPATH/site utilisateur)
            # -B : pas de .pyc   -u : sortie non tamponnée (utile si on tue)
            cmd = [sys.executable or "python3", "-I", "-B", "-u", str(script)]
        else:
            cmd = ["/bin/bash", "--noprofile", "--norc", str(script)]

        env = environnement_propre(dossier)
        # stdin fermé : plus d'`input()` bloquant indéfiniment
        with open(os.devnull, "rb") as devnull, \
                open(Path(dossier) / ".stdout", "wb") as out_f, \
                open(Path(dossier) / ".stderr", "wb") as err_f:
            proc = subprocess.Popen(
                cmd,
                cwd=dossier,
                env=env,
                stdin=devnull,
                stdout=out_f,
                stderr=err_f,
                close_fds=True,
                preexec_fn=_preexec(timeout),
            )
        pid = proc.pid

        timeout_atteint = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timeout_atteint = True
            _tuer_arbre(pid)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        exit_code = proc.returncode
        stdout, tronque_out = _lire_fichier(Path(dossier) / ".stdout")
        stderr, tronque_err = _lire_fichier(Path(dossier) / ".stderr")

        if timeout_atteint:
            note = (f"\n[nexaz] Exécution interrompue après {timeout:.0f} s "
                    f"(timeout dur) — le processus et ses enfants ont été tués.\n")
            stderr = (stderr + note)[:MAX_SORTIE_OCTETS + len(note)]
            exit_code = -9

        return {
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
            "duree_s": round(time.monotonic() - debut, 3),
            "timeout_atteint": timeout_atteint,
            "sortie_tronquee": tronque_out or tronque_err,
            "refus": None,
        }
    finally:
        # ---- nettoyage : le dossier de travail est toujours supprimé ---------
        if proc is not None and proc.poll() is None:
            _tuer_arbre(proc.pid)
        shutil.rmtree(dossier, ignore_errors=True)


def _tuer_arbre(pid: int) -> None:
    """Tue le groupe de processus entier (best effort)."""
    for sig, delai in ((signal.SIGKILL, 0),):
        try:
            os.killpg(os.getpgid(pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if delai:
            time.sleep(delai)


def executer_tache(tache: str, lang: str = "python",
                   timeout: float = TIMEOUT_DEFAUT) -> dict:
    """Wrapper « tâche utilisateur » : refuse AVANT d'appeler le modèle."""
    verifier_securite(tache, etape="tache")
    return executer_code(tache, lang=lang, timeout=timeout)


# ════════════════════════════════════════════════════════════════════════════
# LIMITES CONNUES (à dire honnêtement aux utilisateurs)
# ════════════════════════════════════════════════════════════════════════════
# - Pas de conteneur, pas de namespace, pas de VM : un simple `fork`.
#   Un script partage donc le noyau, l'utilisateur système et le réseau de
#   l'hôte. La liste noire est un filtre textuel — un attaquant qui encode
#   une chaîne (`"r"+"m"`, base64, `chr()`) peut la contourner.
# - `os.setsid()` empêche un enfant de survivre au killpg S'IL ne se
#   détache pas lui-même (`setsid`), ce qu'un attaquant peut faire.
# - Le réseau n'est pas coupé au niveau noyau : on refuse les motifs
#   connus (curl/wget/socket/urllib), on ne bloque pas les sockets.
# - Pas de seccomp, pas d'AppArmor, pas de user namespace.
# - Ce module vise donc : « du code écrit par un LLM qui se trompe »,
#   pas « du code écrit par un adversaire ». Pour ce dernier cas, il
#   faudrait gVisor / Firecracker / un vrai conteneur par tâche.
