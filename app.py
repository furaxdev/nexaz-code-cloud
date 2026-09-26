# -*- coding: utf-8 -*-
"""
app.py — Nexaz Code Cloud (MVP).

Un service web minimal qui :
  1. reçoit une tâche en langage naturel,
  2. demande au modèle local « Nexaz Core » d'écrire le script correspondant,
  3. exécute ce script dans un bac à sable isolé (voir sandbox.py),
  4. renvoie le code généré, la sortie standard, la sortie d'erreur, le code
     de sortie et la durée.

Ce n'est PAS Claude Code Cloud : pas de conteneur par utilisateur, pas de
clone de dépôt, pas d'accès aux fichiers de l'utilisateur. Voir README.md.
"""

import os
import threading
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

import nexaz_client
import sandbox

VERSION = "ncc-1.0.0"
SERVICE = "nexaz-code-cloud"

app = FastAPI(title="Nexaz Code Cloud", version=VERSION)

# Clé publique visible en clair dans la page (sert aussi de marqueur de
# déploiement pour vérifier que l'alias Vercel sert bien la nouvelle build).
MARQUEUR_DEPLOIEMENT = "nexaz-code-cloud-mvp-v1"


# ════════════════════════════════════════════════════════════════════════════
# Stockage en mémoire des tâches (best effort : instance serverless éphémère)
# ════════════════════════════════════════════════════════════════════════════

MAX_TACHES_MEMOIRE = 60
_taches: dict[str, dict] = {}
_verrou = threading.Lock()


def _enregistrer(entree: dict) -> None:
    with _verrou:
        _taches[entree["id"]] = entree
        if len(_taches) > MAX_TACHES_MEMOIRE:
            for cle in sorted(_taches, key=lambda k: _taches[k]["cree_le"])[
                    : len(_taches) - MAX_TACHES_MEMOIRE]:
                _taches.pop(cle, None)


def _maj(tache_id: str, **champs) -> None:
    with _verrou:
        if tache_id in _taches:
            _taches[tache_id].update(champs)


# ════════════════════════════════════════════════════════════════════════════
# Limiteur de débit simple par IP (anti-abus)
# ════════════════════════════════════════════════════════════════════════════

FENETRE_S = 300
MAX_REQUETES = 12
_hits: dict[str, list[float]] = {}
_verrou_hits = threading.Lock()


def _ip_client(request: Request) -> str:
    avant = request.headers.get("x-forwarded-for", "")
    if avant:
        return avant.split(",")[0].strip()
    return request.client.host if request.client else "inconnu"


def _autorise(ip: str) -> bool:
    maintenant = time.time()
    with _verrou_hits:
        passages = [t for t in _hits.get(ip, []) if maintenant - t < FENETRE_S]
        if len(passages) >= MAX_REQUETES:
            _hits[ip] = passages
            return False
        passages.append(maintenant)
        _hits[ip] = passages
        return True


# ════════════════════════════════════════════════════════════════════════════
# Modèles de requête / réponse
# ════════════════════════════════════════════════════════════════════════════

class DemandeTache(BaseModel):
    task: str = Field(..., description="Description en langage naturel")
    lang: str = Field("python", description="python ou bash")
    timeout_s: float = Field(sandbox.TIMEOUT_DEFAUT, ge=sandbox.TIMEOUT_MIN,
                             le=sandbox.TIMEOUT_MAX)
    asynchrone: bool = Field(False, description="Exécution en tâche de fond "
                                               "(best effort sur serverless)")


def _reponse_refus(tache_id: str, refus: dict, duree: float) -> dict:
    return {
        "tache_id": tache_id,
        "refuse": True,
        "raison": f"Tâche refusée par la liste noire : {refus['regle']}",
        "regle": refus["regle"],
        "motif": refus["motif"],
        "etape_refus": refus["etape"],
        "code_genere": None,
        "stdout": "",
        "stderr": "",
        "exit_code": None,
        "duree_s": round(duree, 3),
        "modele": nexaz_client.MODELE,
        "statut": "refusee",
    }


def _traiter(tache_id: str, demande: DemandeTache) -> dict:
    """Pipeline complet : filtre → modèle → filtre → exécution."""
    debut = time.monotonic()

    # ---- FILTRE 1 : la demande elle-même ----------------------------------
    try:
        sandbox.verifier_securite(demande.task, etape="tache")
    except sandbox.RefusSecurite as refus:
        return _reponse_refus(tache_id, refus.as_dict(), time.monotonic() - debut)

    # ---- Étape 2 : génération de code par le modèle local -----------------
    try:
        generation = nexaz_client.generer_code(demande.task, demande.lang)
    except nexaz_client.ModeleIndisponible as exc:
        return {
            "tache_id": tache_id,
            "refuse": False,
            "raison": None,
            "code_genere": None,
            "stdout": "",
            "stderr": "",
            "exit_code": None,
            "duree_s": round(time.monotonic() - debut, 3),
            "erreur": "modele_indisponible",
            "message": f"Modèle local injoignable : {exc}",
            "modele": nexaz_client.MODELE,
            "statut": "erreur",
        }

    code = generation["code"]
    temps_modele = generation["duree_modele_s"]

    # ---- FILTRE 2 : le code produit ---------------------------------------
    try:
        sandbox.verifier_securite(code, etape="code")
    except sandbox.RefusSecurite as refus:
        reponse = _reponse_refus(tache_id, refus.as_dict(), time.monotonic() - debut)
        reponse["code_genere"] = code
        reponse["duree_modele_s"] = temps_modele
        return reponse

    # ---- Étape 4 : exécution isolée ---------------------------------------
    try:
        resultat = sandbox.executer_code(code, demande.lang, demande.timeout_s)
    except sandbox.RefusSecurite as refus:
        reponse = _reponse_refus(tache_id, refus.as_dict(), time.monotonic() - debut)
        reponse["code_genere"] = code
        return reponse
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "tache_id": tache_id,
        "refuse": False,
        "raison": None,
        "code_genere": code,
        "stdout": resultat["stdout"],
        "stderr": resultat["stderr"],
        "exit_code": resultat["exit_code"],
        "duree_s": round(time.monotonic() - debut, 3),
        "duree_execution_s": resultat["duree_s"],
        "duree_modele_s": temps_modele,
        "timeout_atteint": resultat["timeout_atteint"],
        "sortie_tronquee": resultat["sortie_tronquee"],
        "lang": demande.lang,
        "modele": nexaz_client.MODELE,
        "usage_modele": generation.get("usage", {}),
        "statut": "terminee",
    }


# ════════════════════════════════════════════════════════════════════════════
# Routes
# ════════════════════════════════════════════════════════════════════════════

@app.get("/sante")
def sante():
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "deploiement": MARQUEUR_DEPLOIEMENT,
        "modele": nexaz_client.MODELE,
        "modele_joignable": nexaz_client.modele_joignable(),
        "langages": sorted(sandbox.LANGAGES),
        "timeout_defaut_s": sandbox.TIMEOUT_DEFAUT,
        "max_sortie_octets": sandbox.MAX_SORTIE_OCTETS,
        "isolation": "processus enfant jetable (pas de conteneur)",
        "horodatage": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@app.post("/api/task")
def creer_tache(demande: DemandeTache, request: Request):
    ip = _ip_client(request)
    if not _autorise(ip):
        raise HTTPException(
            status_code=429,
            detail=f"Trop de tâches (max {MAX_REQUETES} par {FENETRE_S // 60} min)")

    if not demande.task or not demande.task.strip():
        raise HTTPException(status_code=400, detail="Le champ 'task' est vide")
    if len(demande.task) > 800:
        raise HTTPException(status_code=400, detail="Tâche trop longue (max 800 caractères)")
    if demande.lang not in sandbox.LANGAGES:
        raise HTTPException(status_code=400,
                            detail="Langage non supporté (python ou bash)")

    tache_id = uuid.uuid4().hex[:12]
    _enregistrer({
        "id": tache_id,
        "tache": demande.task,
        "lang": demande.lang,
        "statut": "en_cours",
        "cree_le": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resultat": None,
    })

    if demande.asynchrone:
        def _fil():
            try:
                resultat = _traiter(tache_id, demande)
            except Exception as exc:  # pragma: no cover
                resultat = {"statut": "erreur", "message": repr(exc)}
            _maj(tache_id, statut=resultat.get("statut", "terminee"),
                 resultat=resultat)
        threading.Thread(target=_fil, daemon=True).start()
        return JSONResponse(status_code=202, content={
            "tache_id": tache_id,
            "statut": "en_cours",
            "poll": f"/api/task/{tache_id}",
            "note": "Mode asynchrone best-effort : sur un hébergement "
                    "serverless l'instance peut geler après la réponse.",
        })

    resultat = _traiter(tache_id, demande)
    _maj(tache_id, statut=resultat.get("statut", "terminee"), resultat=resultat)

    if resultat.get("refuse"):
        return JSONResponse(status_code=403, content=resultat)
    if resultat.get("erreur") == "modele_indisponible":
        return JSONResponse(status_code=503, content=resultat)
    return JSONResponse(status_code=200, content=resultat)


@app.get("/api/task/{tache_id}")
def lire_tache(tache_id: str):
    with _verrou:
        entree = _taches.get(tache_id)
    if not entree:
        return JSONResponse(status_code=404, content={
            "tache_id": tache_id,
            "statut": "inconnue",
            "message": "Tâche inconnue de cette instance (le stockage en mémoire "
                       "est éphémère sur un hébergement serverless).",
        })
    return {
        "tache_id": tache_id,
        "tache": entree["tache"],
        "lang": entree["lang"],
        "statut": entree["statut"],
        "cree_le": entree["cree_le"],
        "resultat": entree["resultat"],
    }


@app.get("/api/tasks")
def lister_taches():
    with _verrou:
        entrees = [
            {"tache_id": e["id"], "tache": e["tache"], "lang": e["lang"],
             "statut": e["statut"], "cree_le": e["cree_le"]}
            for e in sorted(_taches.values(), key=lambda x: x["cree_le"], reverse=True)
        ]
    return {"nombre": len(entrees), "taches": entrees}


@app.get("/", response_class=HTMLResponse)
def accueil():
    return HTMLResponse(content=PAGE_HTML)


# ════════════════════════════════════════════════════════════════════════════
# Interface web — HTML inline (un projet FastAPI sur Vercel ne sert pas /static/)
# ════════════════════════════════════════════════════════════════════════════

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="deploiement" content="nexaz-code-cloud-mvp-v1">
<title>Nexaz Code Cloud — exécute du code dans un bac à sable</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  :root{
    --violet:#6d5cf7; --cyan:#22d3ee; --fond:#08080c; --carte:#12121a;
    --bordure:rgba(255,255,255,.08); --texte:#e9e9f0; --doux:#9a9aae;
    --vert:#34d399; --rouge:#f87171;
  }
  body{
    font-family:'Inter',-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    background:var(--fond); color:var(--texte); min-height:100vh;
    -webkit-font-smoothing:antialiased; line-height:1.55;
    background-image:
      radial-gradient(60rem 40rem at 15% -10%, rgba(109,92,247,.22), transparent 60%),
      radial-gradient(45rem 35rem at 95% 0%, rgba(34,211,238,.14), transparent 55%);
    background-attachment:fixed;
  }
  .enveloppe{max-width:920px;margin:0 auto;padding:28px 18px 64px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:8px}
  .logo{
    width:46px;height:46px;border-radius:14px;flex:0 0 auto;
    background:linear-gradient(135deg,var(--violet),var(--cyan));
    display:grid;place-items:center;font-weight:700;font-size:17px;color:#0b0b12;
    box-shadow:0 8px 26px rgba(109,92,247,.38)
  }
  .titres h1{font-size:21px;font-weight:700;letter-spacing:-.02em}
  .titres p{font-size:13px;color:var(--doux)}
  .pill{
    margin-left:auto;display:inline-flex;align-items:center;gap:7px;
    font-size:12px;color:var(--doux);border:1px solid var(--bordure);
    background:rgba(255,255,255,.03);padding:6px 12px;border-radius:999px
  }
  .point{width:8px;height:8px;border-radius:50%;background:var(--vert);
    box-shadow:0 0 10px var(--vert)}
  .point.hors{background:var(--rouge);box-shadow:0 0 10px var(--rouge)}
  .carte{
    background:linear-gradient(180deg,rgba(255,255,255,.035),rgba(255,255,255,0)) ,
      var(--carte);
    border:1px solid var(--bordure);border-radius:18px;padding:18px;
    margin-top:16px;box-shadow:0 12px 34px rgba(0,0,0,.32)
  }
  label{display:block;font-size:12px;font-weight:600;letter-spacing:.04em;
    text-transform:uppercase;color:var(--doux);margin-bottom:9px}
  textarea{
    width:100%;min-height:92px;resize:vertical;background:#0d0d14;color:var(--texte);
    border:1px solid var(--bordure);border-radius:13px;padding:13px 14px;
    font-family:inherit;font-size:15px;line-height:1.5;outline:none;transition:border .15s
  }
  textarea:focus{border-color:rgba(109,92,247,.65);box-shadow:0 0 0 3px rgba(109,92,247,.16)}
  .barre{display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap}
  select{
    background:#0d0d14;color:var(--texte);border:1px solid var(--bordure);
    border-radius:11px;padding:11px 13px;font-family:inherit;font-size:14px;outline:none
  }
  button{
    font-family:inherit;font-size:14.5px;font-weight:600;cursor:pointer;border:0;
    border-radius:11px;padding:12px 22px;color:#0b0b12;
    background:linear-gradient(135deg,var(--violet),var(--cyan));
    transition:transform .12s,opacity .12s;box-shadow:0 8px 22px rgba(109,92,247,.3)
  }
  button:hover:not(:disabled){transform:translateY(-1px)}
  button:disabled{opacity:.5;cursor:not-allowed;transform:none}
  .exemples{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
  .puce{
    font-size:12.5px;color:var(--doux);border:1px dashed var(--bordure);
    background:rgba(255,255,255,.02);padding:6px 11px;border-radius:999px;cursor:pointer;
    transition:color .15s,border-color .15s
  }
  .puce:hover{color:var(--texte);border-color:rgba(109,92,247,.6)}
  .chrono{
    display:none;align-items:center;gap:10px;margin-top:14px;font-size:13.5px;color:var(--doux)
  }
  .chrono.actif{display:flex}
  .roue{width:15px;height:15px;border-radius:50%;border:2px solid rgba(255,255,255,.15);
    border-top-color:var(--cyan);animation:tour 0.8s linear infinite}
  @keyframes tour{to{transform:rotate(360deg)}}
  .entete-resultat{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}
  .entete-resultat h2{font-size:15px;font-weight:600}
  .etiquette{font-size:11.5px;font-weight:600;padding:4px 10px;border-radius:999px;
    border:1px solid var(--bordure);color:var(--doux);font-family:'JetBrains Mono',monospace}
  .etiquette.ok{color:var(--vert);border-color:rgba(52,211,153,.35);background:rgba(52,211,153,.08)}
  .etiquette.ko{color:var(--rouge);border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.08)}
  pre{
    background:#0a0a10;border:1px solid var(--bordure);border-radius:13px;
    padding:15px;overflow:auto;font-family:'JetBrains Mono',ui-monospace,Menlo,monospace;
    font-size:13px;line-height:1.62;max-height:420px;tab-size:4
  }
  code{font-family:'JetBrains Mono',ui-monospace,Menlo,monospace}
  .term{background:#06060a;border-color:rgba(34,211,238,.16)}
  .term .ligne{white-space:pre-wrap;word-break:break-word}
  .term .err{color:#fca5a5}
  .term .sys{color:var(--cyan)}
  .vide{color:#57576b;font-style:italic}
  .k{color:#c084fc}.s{color:#7dd3a8}.c{color:#6b7280;font-style:italic}
  .n{color:#fbbf24}.f{color:#67b8f7}
  .refus{border-color:rgba(248,113,113,.4);background:linear-gradient(180deg,rgba(248,113,113,.09),transparent),var(--carte)}
  .refus h2{color:var(--rouge)}
  .grille-limites{margin-top:10px;padding-left:18px;color:var(--doux);font-size:13px}
  .grille-limites li{margin:5px 0}
  footer{margin-top:26px;font-size:12.5px;color:#6f6f85;text-align:center;line-height:1.7}
  footer code{color:var(--doux)}
  @media (max-width:600px){
    .enveloppe{padding:20px 13px 52px}
    .titres h1{font-size:18px}
    .pill{margin-left:0;width:100%}
    .carte{padding:15px;border-radius:16px}
    button{width:100%}
    select{flex:1}
    pre{font-size:12px;padding:13px}
  }
</style>
</head>
<body>
<div class="enveloppe">

  <header>
    <div class="logo">Nx</div>
    <div class="titres">
      <h1>Nexaz Code Cloud</h1>
      <p>Décris une tâche → le modèle écrit le code → il s'exécute dans un bac à sable.</p>
    </div>
    <span class="pill" id="pill">
      <span class="point" id="point"></span><span id="etat-modele">vérification du modèle…</span>
    </span>
  </header>

  <section class="carte">
    <label for="task">Ta tâche</label>
    <textarea id="task" placeholder="Ex : affiche les nombres de 1 à 5"></textarea>
    <div class="barre">
      <select id="lang">
        <option value="python">Python</option>
        <option value="bash">Bash</option>
      </select>
      <button id="run">▶ Exécuter</button>
    </div>
    <div class="exemples">
      <span class="puce" data-t="affiche les nombres de 1 a 5">nombres de 1 à 5</span>
      <span class="puce" data-t="affiche les 10 premiers nombres premiers">nombres premiers</span>
      <span class="puce" data-t="calcule la factorielle de 12">factorielle de 12</span>
      <span class="puce" data-t="affiche la table de multiplication de 7">table de 7</span>
      <span class="puce" data-t="compte a rebours de 5 a 1">compte à rebours</span>
    </div>
    <div class="chrono" id="chrono"><span class="roue"></span>
      <span id="chrono-texte">Exécution…</span>
    </div>
  </section>

  <section id="zone-refus"></section>
  <section id="zone-resultat"></section>

  <section class="carte" id="carte-limites">
    <h2 style="font-size:15px;margin-bottom:6px">Ce que ce MVP est — et n'est pas</h2>
    <ul class="grille-limites">
      <li>Chaque tâche tourne dans <b>son propre dossier temporaire</b>, supprimé après coup, avec un <b>timeout dur</b> et un environnement vidé de tout secret.</li>
      <li>Ce n'est <b>pas un conteneur par utilisateur</b> (impossible sur cette infra) : c'est un processus enfant jetable, filtré par une liste noire.</li>
      <li>Pas de multi-session, pas de clone de dépôt, <b>aucun accès aux fichiers de ton ordinateur</b>.</li>
      <li>Le modèle est un <b>Qwen2.5-3B en CPU</b> (~11 tokens/s) : le code est simple, parfois imparfait.</li>
    </ul>
  </section>

  <footer>
    Nexaz Code Cloud · MVP <code id="ver">ncc-1.0.0</code> · API :
    <code>POST /api/task</code>, <code>GET /api/task/{id}</code>, <code>GET /sante</code>
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
let chronoId = null, t0 = 0;

function majChrono(){
  const s = (performance.now() - t0) / 1000;
  $('chrono-texte').textContent = 'Exécution… ' + s.toFixed(1) + ' s';
}

async function verifierSante(){
  try{
    const r = await fetch('/sante');
    const d = await r.json();
    $('point').className = 'point' + (d.modele_joignable ? '' : ' hors');
    $('etat-modele').textContent = d.modele_joignable
      ? 'modèle ' + d.modele + ' en ligne' : 'modèle hors ligne';
    if (d.version) $('ver').textContent = d.version;
  }catch(e){
    $('point').className = 'point hors';
    $('etat-modele').textContent = 'service injoignable';
  }
}

function echapper(t){
  return t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function colorer(code){
  // Analyse en un seul passage : chaque jeton est remplace une seule fois,
  // donc le balisage insere ne peut pas etre re-analyse (pas de HTML casse).
  const MOTS = /\b(def|return|if|elif|else|for|while|in|import|from|as|class|try|except|finally|with|lambda|True|False|None|and|or|not|break|continue|pass|print|range|len|int|str|float|list|dict|set|echo|fi|done|then|do|function)\b/;
  const JETON = /(#[^\n]*)|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|\b(\d+(?:\.\d+)?)\b|([A-Za-z_]\w*(?=\s*\())/g;
  let sortie = '', dernier = 0, m;
  while ((m = JETON.exec(code)) !== null){
    sortie += echapper(code.slice(dernier, m.index));
    let cls = m[1] ? 'c' : m[2] ? 's' : m[3] ? 'n' : 'f';
    if (!m[1] && !m[2] && MOTS.test(m[0])) cls = 'k';
    sortie += '<span class="' + cls + '">' + echapper(m[0]) + '</span>';
    dernier = m.index + m[0].length;
  }
  return sortie + echapper(code.slice(dernier));
}

function etiquette(txt, cls){
  return '<span class="etiquette ' + (cls||'') + '">' + echapper(String(txt)) + '</span>';
}

function afficherRefus(d){
  $('zone-resultat').innerHTML = '';
  const motif = d.motif ? ' <code>' + echapper(d.motif) + '</code>' : '';
  let html = '<div class="carte refus">';
  html += '<div class="entete-resultat"><h2>⛔ Tâche refusée</h2>' +
          etiquette('liste noire', 'ko') + etiquette('durée ' + d.duree_s + ' s') + '</div>';
  html += '<p style="font-size:14px">' + echapper(d.raison) + motif + '</p>';
  html += '<p style="font-size:12.5px;color:var(--doux);margin-top:9px">' +
          'Filtre appliqué à l\'étape : <b>' + echapper(d.etape_refus || 'code') + '</b>. ' +
          'Le code n\'a jamais été exécuté.</p>';
  if (d.code_genere){
    html += '<pre style="margin-top:12px">' + colorer(d.code_genere) + '</pre>';
  }
  html += '</div>';
  $('zone-refus').innerHTML = html;
}

function afficherResultat(d){
  $('zone-refus').innerHTML = '';
  const okCls = d.exit_code === 0 ? 'ok' : 'ko';
  let html = '<div class="carte">';
  html += '<div class="entete-resultat"><h2>🧩 Code généré par ' + echapper(d.modele || '') + '</h2>' +
          etiquette(d.lang || '') +
          (d.duree_modele_s ? etiquette('modèle ' + d.duree_modele_s + ' s') : '') +
          '</div>';
  html += '<pre>' + colorer(d.code_genere || '') + '</pre></div>';

  html += '<div class="carte"><div class="entete-resultat"><h2>▸ Sortie</h2>' +
          etiquette('exit ' + d.exit_code, okCls) +
          etiquette('total ' + d.total + ' s') +
          (d.duree_execution_s !== undefined ? etiquette('exécution ' + d.duree_execution_s + ' s') : '') +
          (d.timeout_atteint ? etiquette('timeout', 'ko') : '') +
          (d.sortie_tronquee ? etiquette('sortie tronquée') : '') +
          '</div><pre class="term">';
  let corps = '';
  if (d.stdout) corps += '<span class="ligne">' + echapper(d.stdout) + '</span>';
  if (d.stderr) corps += '<span class="ligne err">' + echapper(d.stderr) + '</span>';
  html += corps || '<span class="vide">(aucune sortie)</span>';
  html += '</pre><p style="font-size:11.5px;color:#57576b;margin-top:9px">tâche <code>' +
          echapper(d.tache_id) + '</code> · rejouable via GET /api/task/' + echapper(d.tache_id) + '</p></div>';
  $('zone-resultat').innerHTML = html;
}

async function lancer(){
  const task = $('task').value.trim();
  if (!task) { $('task').focus(); return; }
  $('run').disabled = true;
  $('zone-refus').innerHTML = '';
  $('zone-resultat').innerHTML = '';
  $('chrono').classList.add('actif');
  t0 = performance.now();
  majChrono();
  chronoId = setInterval(majChrono, 100);

  try{
    const r = await fetch('/api/task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({task: task, lang: $('lang').value})
    });
    const d = await r.json();
    const total = ((performance.now() - t0) / 1000).toFixed(2);
    if (r.status === 403 || d.refuse){ d.duree_s = d.duree_s || total; afficherRefus(d); }
    else if (r.status === 503){
      $('zone-refus').innerHTML = '<div class="carte refus"><h2>⚠️ Modèle local injoignable</h2>' +
        '<p style="font-size:14px;margin-top:8px">' + echapper(d.message || '') + '</p></div>';
    }
    else if (!r.ok){
      $('zone-refus').innerHTML = '<div class="carte refus"><h2>Erreur ' + r.status + '</h2>' +
        '<p style="font-size:14px;margin-top:8px">' + echapper(d.detail || 'Erreur inconnue') + '</p></div>';
    }
    else { d.total = total; afficherResultat(d); }
  }catch(e){
    $('zone-refus').innerHTML = '<div class="carte refus"><h2>Réseau</h2>' +
      '<p style="font-size:14px;margin-top:8px">' + echapper(String(e)) + '</p></div>';
  }finally{
    clearInterval(chronoId);
    $('chrono').classList.remove('actif');
    $('run').disabled = false;
  }
}

$('run').addEventListener('click', lancer);
$('task').addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) lancer();
});
document.querySelectorAll('.puce').forEach(p => p.addEventListener('click', () => {
  $('task').value = p.dataset.t; $('task').focus();
}));
verifierSante();
</script>
</body>
</html>
"""
