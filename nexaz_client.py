# -*- coding: utf-8 -*-
"""
nexaz_client.py — client minimal du modèle local « Nexaz Core ».

Nexaz Core est un llama.cpp (Qwen2.5-3B-Instruct, CPU) exposé en API
compatible OpenAI sur http://127.0.0.1:8100/v1/chat/completions.

Le conteneur qui héberge le modèle n'est PAS joignable depuis Internet :
on passe donc par le tunnel Cloudflare maintenu par le watchdog du poste
(variable d'environnement NEXAZ_CORE_URL). Le contexte du modèle est de
1024 tokens : le prompt est volontairement minimal.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request

MODELE = "nexaz-core"
TIMEOUT_MODELE = 45.0        # secondes — reste sous le maxDuration serverless
MAX_TOKENS = 256
TEMPERATURE = 0.2

# URL de secours si aucune variable d'environnement n'est fournie.
_URL_PAR_DEFAUT = ""


def _urls_disponibles() -> list[str]:
    """Liste ordonnée des bases d'API à essayer.

    NEXAZ_CORE_URLS (liste séparée par des virgules) est prioritaire, puis
    NEXAZ_CORE_URL, puis la valeur de secours compilée.
    """
    urls: list[str] = []
    brut = os.environ.get("NEXAZ_CORE_URLS", "")
    urls += [u.strip().rstrip("/") for u in brut.split(",") if u.strip()]
    unique = os.environ.get("NEXAZ_CORE_URL", "").strip().rstrip("/")
    if unique:
        urls.append(unique)
    if _URL_PAR_DEFAUT:
        urls.append(_URL_PAR_DEFAUT.rstrip("/"))
    # dédoublonne en gardant l'ordre
    vus, sortie = set(), []
    for u in urls:
        if u not in vus:
            vus.add(u)
            sortie.append(u)
    return sortie


PROMPT_TEMPLATE = (
    "Tu es un generateur de code. Ecris un script {lang} qui realise la tache.\n"
    "Le script doit afficher son resultat.\n"
    "Reponds uniquement avec le code, sans explication ni balise markdown.\n"
    "Tache: {tache}\n"
    "Code {lang}:\n"
)


class ModeleIndisponible(Exception):
    """Le modèle n'a pas pu être joint (tunnel arrêté, timeout, mauvaise URL)."""


def nettoyer_code(brut: str) -> str:
    """Retire les éventuelles balises markdown autour du code généré."""
    if not brut:
        return ""
    texte = brut.strip()
    # ```lang ... ``` ou ``` ... ```
    bloc = re.search(r"```[a-zA-Z0-9_+-]*\s*\n(.*?)```", texte, re.DOTALL)
    if bloc:
        texte = bloc.group(1).strip()
    texte = re.sub(r"^\s*```[a-zA-Z0-9_+-]*\s*$", "", texte, flags=re.MULTILINE)
    return texte.strip()


def _appel(url: str, tache: str, lang: str) -> dict:
    prompt = PROMPT_TEMPLATE.format(lang=lang, tache=tache)
    corps = json.dumps({
        "model": MODELE,          # ⚠️ doit valoir exactement "nexaz-core"
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "stream": False,
    }).encode("utf-8")

    requete = urllib.request.Request(
        f"{url}/v1/chat/completions",
        data=corps,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(requete, timeout=TIMEOUT_MODELE) as reponse:
        charge = json.loads(reponse.read().decode("utf-8"))

    choix = charge.get("choices") or []
    if not choix:
        raise ModeleIndisponible("réponse du modèle sans 'choices'")
    message = choix[0].get("message") or {}
    contenu = message.get("content")
    if not isinstance(contenu, str) or not contenu.strip():
        raise ModeleIndisponible("le modèle a renvoyé un contenu vide")

    return {
        "code": nettoyer_code(contenu),
        "brut": contenu,
        "usage": charge.get("usage") or {},
    }


def generer_code(tache: str, lang: str = "python") -> dict:
    """Demande au modèle local d'écrire un script.

    Essaie successivement les URLs connues. Lève ModeleIndisponible si
    aucune ne répond.
    """
    urls = _urls_disponibles()
    if not urls:
        raise ModeleIndisponible(
            "aucune URL de modèle configurée (NEXAZ_CORE_URL absente)")

    erreurs = []
    for url in urls:
        debut = time.monotonic()
        try:
            resultat = _appel(url, tache, lang)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError,
                TimeoutError, json.JSONDecodeError, ModeleIndisponible) as exc:
            erreurs.append(f"{url.split('//')[-1][:28]}:{type(exc).__name__}")
            continue
        resultat["duree_modele_s"] = round(time.monotonic() - debut, 3)
        resultat["source"] = url
        return resultat

    raise ModeleIndisponible("modèle injoignable — " + ", ".join(erreurs))


def modele_joignable(timeout: float = 6.0) -> bool:
    """Ping rapide de /v1/models (utilisé par /sante)."""
    for url in _urls_disponibles():
        try:
            with urllib.request.urlopen(f"{url}/v1/models", timeout=timeout) as r:
                if MODELE in r.read().decode("utf-8", errors="replace"):
                    return True
        except Exception:
            continue
    return False
