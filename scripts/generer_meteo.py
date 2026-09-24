#!/usr/bin/env python3
"""
Génère chaque matin la météo des principales villes du Québec et de l'Ontario
à partir des données officielles d'Environnement Canada (meteo.gc.ca).

Produit dans le dossier docs/ (publié par GitHub Pages) :
  - index.html              : la page web
  - meteo-du-jour.pdf       : le PDF du jour
  - archives/meteo-AAAA-MM-JJ.pdf : l'historique (30 derniers jours)
  - donnees.json            : les données brutes

Utilisation :  python scripts/generer_meteo.py [--force]
Sans --force, le script ne fait rien avant 5 h (heure de l'Est) ou si le
rapport du jour existe déjà (évite les doublons dus aux deux horaires été/hiver).
"""

import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

# ----------------------------------------------------------------------------
# Villes (identifiants de l'API d'Environnement Canada). Pour en ajouter une,
# cherche son code sur https://api.weather.gc.ca/collections/citypageweather-realtime/items
# ----------------------------------------------------------------------------
VILLES = {
    "Québec": [
        ("qc-147", "Montréal"), ("qc-133", "Québec"), ("qc-76", "Laval"),
        ("qc-126", "Gatineau"), ("qc-109", "Longueuil"), ("qc-136", "Sherbrooke"),
        ("qc-166", "Saguenay"), ("qc-78", "Lévis"), ("qc-130", "Trois-Rivières"),
        ("qc-28", "Saint-Jean-sur-Richelieu"), ("qc-13", "Saint-Jérôme"),
        ("qc-2", "Drummondville"), ("qc-5", "Granby"), ("qc-22", "Saint-Hyacinthe"),
        ("qc-138", "Rimouski"), ("qc-148", "Rouyn-Noranda"), ("qc-149", "Val-d'Or"),
        ("qc-141", "Sept-Îles"), ("qc-160", "Baie-Comeau"), ("qc-101", "Gaspé"),
    ],
    "Ontario": [
        ("on-143", "Toronto"), ("on-118", "Ottawa"), ("on-24", "Mississauga"),
        ("on-4", "Brampton"), ("on-77", "Hamilton"), ("on-137", "London"),
        ("on-85", "Markham"), ("on-64", "Vaughan"), ("on-82", "Kitchener-Waterloo"),
        ("on-94", "Windsor"), ("on-117", "Oshawa"), ("on-107", "St. Catharines"),
        ("on-151", "Barrie"), ("on-5", "Guelph"), ("on-69", "Kingston"),
        ("on-40", "Grand Sudbury"), ("on-100", "Thunder Bay"),
        ("on-162", "Sault Ste. Marie"), ("on-139", "North Bay"), ("on-127", "Timmins"),
    ],
}

API = "https://api.weather.gc.ca/collections/citypageweather-realtime/items/{id}?f=json&lang=fr"
FUSEAU = ZoneInfo("America/Toronto")
RACINE = Path(__file__).resolve().parent.parent
DOCS = RACINE / "docs"
ARCHIVES = DOCS / "archives"
JOURS_ARCHIVES = 30

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
        "septembre", "octobre", "novembre", "décembre"]


def date_longue(d):
    return f"{JOURS[d.weekday()]} {d.day}{'er' if d.day == 1 else ''} {MOIS[d.month - 1]} {d.year}"


# ----------------------------------------------------------------------------
# Lecture des données
# ----------------------------------------------------------------------------
def fr(obj, *chemin):
    """Descend dans le JSON et renvoie la valeur française (ou None)."""
    for cle in chemin:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(cle)
    if isinstance(obj, dict) and ("fr" in obj or "en" in obj):
        return obj.get("fr", obj.get("en"))
    return obj


def heure_locale(iso):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(FUSEAU)
    except ValueError:
        return None


def telecharger(id_ville):
    for essai in range(4):
        try:
            r = requests.get(API.format(id=id_ville), timeout=40,
                             headers={"User-Agent": "meteo-quebec-ontario (GitHub Pages)"})
            r.raise_for_status()
            return r.json()
        except Exception as e:  # réseau capricieux : on réessaie
            if essai == 3:
                print(f"  ! {id_ville} : échec ({e})", file=sys.stderr)
                return None
            time.sleep(5 * (essai + 1))


def temp_de(periode, classe):
    for t in (fr(periode, "temperatures") or {}).get("temperature", []) or []:
        if fr(t, "class") == classe:
            return fr(t, "value")
    return None


def prob_precip(periode):
    pop = fr(periode, "abbreviatedForecast", "pop")
    if isinstance(pop, dict):
        pop = fr(pop, "value")
    if pop not in (None, ""):
        return pop
    m = re.search(r"(\d+)\s*pour\s*cent", fr(periode, "cloudPrecip") or "")
    return int(m.group(1)) if m else None


def resume_periode(periode):
    if not periode:
        return None
    return {
        "nom": fr(periode, "period", "textForecastName"),
        "conditions": fr(periode, "abbreviatedForecast", "textSummary"),
        "icone": fr(periode, "abbreviatedForecast", "icon", "url"),
        "max": temp_de(periode, "high"),
        "min": temp_de(periode, "low"),
        "pop": prob_precip(periode),
        "vent": fr(periode, "winds", "textSummary"),
        "uv": fr(periode, "uv", "textSummary"),
        "texte": fr(periode, "textSummary"),
    }


def extraire(id_ville, nom, brut):
    base = {"id": id_ville, "nom": nom, "ok": False,
            "lien": f"https://meteo.gc.ca/fr/location/index.html"}
    if not brut:
        return base
    p = brut.get("properties", {})
    base["lien"] = fr(p, "url") or base["lien"]
    cc = p.get("currentConditions") or {}
    previsions = (p.get("forecastGroup") or {}).get("forecasts") or []

    # Le bulletin du matin commence par « Aujourd'hui » (max), puis « Ce soir et cette nuit » (min).
    # S'il commence déjà par la nuit (bulletin de la veille), il n'y a plus de période de jour.
    jour = nuit = None
    if previsions:
        if temp_de(previsions[0], "high") is not None:
            jour = previsions[0]
            nuit = previsions[1] if len(previsions) > 1 else None
        else:
            nuit = previsions[0]

    normales = {fr(t, "class"): fr(t, "value")
                for t in ((p.get("forecastGroup") or {}).get("regionalNormals") or {}).get("temperature", []) or []}
    vent_actuel = None
    if cc.get("wind"):
        vit, dirn = fr(cc, "wind", "speed", "value"), fr(cc, "wind", "direction", "value")
        raf = fr(cc, "wind", "gust", "value")
        if vit is not None:
            vent_actuel = f"{dirn or ''} {vit} km/h" + (f" (rafales {raf})" if raf else "")

    alertes = [{"titre": fr(a, "description"), "niveau": fr(a, "alertColourLevel"), "lien": fr(a, "url")}
               for a in (p.get("warnings") or [])]
    lever, coucher = heure_locale(fr(p, "riseSet", "sunrise")), heure_locale(fr(p, "riseSet", "sunset"))
    emis = heure_locale(fr(p, "forecastGroup", "timestamp"))

    base.update({
        "ok": True,
        "actuel": {
            "temp": fr(cc, "temperature", "value"),
            "conditions": fr(cc, "condition"),
            "humidite": fr(cc, "relativeHumidity", "value"),
            "vent": vent_actuel,
            "station": fr(cc, "station", "value"),
        },
        "jour": resume_periode(jour),
        "nuit": resume_periode(nuit),
        "normales": {"max": normales.get("high"), "min": normales.get("low")},
        "alertes": alertes,
        "lever": lever.strftime("%H:%M") if lever else None,
        "coucher": coucher.strftime("%H:%M") if coucher else None,
        "emis": emis.strftime("%H:%M") if emis else None,
    })
    return base


def recolter():
    taches = [(prov, i, n) for prov, liste in VILLES.items() for i, n in liste]
    with ThreadPoolExecutor(max_workers=6) as ex:
        bruts = list(ex.map(lambda t: telecharger(t[1]), taches))
    resultat = {prov: [] for prov in VILLES}
    for (prov, i, n), brut in zip(taches, bruts):
        resultat[prov].append(extraire(i, n, brut))
    return resultat


# ----------------------------------------------------------------------------
# Mise en forme
# ----------------------------------------------------------------------------
def deg(v):
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
        return f"{round(f)}°" if abs(f - round(f)) < 0.05 else f"{f:.1f}°".replace(".", ",")
    except (TypeError, ValueError):
        return f"{v}°"


def pct(v):
    return "—" if v in (None, "") else f"{v} %"


# ------------------------------- PDF ---------------------------------------
def generer_pdf(donnees, maintenant, chemin):
    doc = SimpleDocTemplate(str(chemin), pagesize=landscape(letter),
                            leftMargin=1.2 * cm, rightMargin=1.2 * cm,
                            topMargin=1.2 * cm, bottomMargin=1.2 * cm,
                            title=f"Météo du {date_longue(maintenant)}",
                            author="Environnement Canada (meteo.gc.ca)")
    bleu = colors.HexColor("#1f4e79")
    st_titre = ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=bleu, spaceAfter=2)
    st_sous = ParagraphStyle("s", fontName="Helvetica", fontSize=9, textColor=colors.HexColor("#555555"), spaceAfter=8)
    st_h2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=13, textColor=bleu, spaceBefore=6, spaceAfter=5)
    st_cel = ParagraphStyle("c", fontName="Helvetica", fontSize=8, leading=9.5, alignment=TA_LEFT)
    st_cel_b = ParagraphStyle("cb", parent=st_cel, fontName="Helvetica-Bold")
    st_detail = ParagraphStyle("d", fontName="Helvetica", fontSize=8, leading=10)
    st_alerte = ParagraphStyle("a", parent=st_cel, textColor=colors.HexColor("#b00020"), fontName="Helvetica-Bold")

    def P(t, s=st_cel):
        return Paragraph(html.escape(str(t)) if t not in (None, "") else "—", s)

    elements = [
        Paragraph(f"Météo du {date_longue(maintenant)}", st_titre),
        Paragraph(f"Québec et Ontario · Généré le {maintenant.strftime('%d/%m/%Y à %H:%M')} (heure de l'Est) · "
                  "Source : Environnement et Changement climatique Canada — meteo.gc.ca", st_sous),
    ]

    entetes = ["Ville", "Actuel", "Jour\nmax", "Conditions (jour)", "Nuit\nmin", "Conditions (nuit)",
               "Prob.\nprécip.", "Normales\nmax / min", "Soleil\nlever / coucher", "Alertes"]
    largeurs = [4.2, 1.4, 1.2, 4.4, 1.2, 4.4, 1.6, 1.9, 2.2, 3.0]

    for num, (prov, villes) in enumerate(donnees.items()):
        lignes = [[P(e.replace("\n", " "), st_cel_b) for e in entetes]]
        fusions = []
        for v in villes:
            if not v["ok"]:
                fusions.append(("SPAN", (1, len(lignes)), (-1, len(lignes))))
                lignes.append([P(v["nom"], st_cel_b), P("Données indisponibles pour le moment")] + [""] * 8)
                continue
            j, n = v["jour"] or {}, v["nuit"] or {}
            pops = [x for x in (j.get("pop"), n.get("pop")) if x not in (None, "")]
            alertes = "; ".join(a["titre"] for a in v["alertes"] if a.get("titre"))
            lignes.append([
                P(v["nom"], st_cel_b),
                P(deg(v["actuel"]["temp"])),
                P(deg(j.get("max")) if j else "—", st_cel_b),
                P(j.get("conditions") if j else "(période de jour terminée)"),
                P(deg(n.get("min")), st_cel_b),
                P(n.get("conditions")),
                P(" / ".join(pct(x) for x in pops) if pops else "—"),
                P(f"{deg(v['normales']['max'])} / {deg(v['normales']['min'])}"),
                P(f"{v['lever'] or '—'} / {v['coucher'] or '—'}"),
                P(alertes or "Aucune", st_alerte if alertes else st_cel),
            ])
        t = Table(lignes, colWidths=[w * cm for w in largeurs], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#dce6f1")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fb")]),
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#c4cfdb")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ] + fusions))
        if num:
            elements.append(PageBreak())
        elements += [Paragraph(prov, st_h2), t, Spacer(1, 6)]

    # Détail complet par ville
    elements += [PageBreak(), Paragraph("Prévisions détaillées", st_titre),
                 Paragraph("Texte intégral des prévisions d'Environnement Canada pour aujourd'hui et cette nuit.", st_sous)]
    for prov, villes in donnees.items():
        elements.append(Paragraph(prov, st_h2))
        for v in villes:
            if not v["ok"]:
                continue
            bloc = [Paragraph(f"<b>{html.escape(v['nom'])}</b> — actuellement {deg(v['actuel']['temp'])}, "
                              f"{html.escape(v['actuel']['conditions'] or '—')}"
                              + (f", humidité {v['actuel']['humidite']} %" if v['actuel']['humidite'] is not None else "")
                              + (f", vent {html.escape(v['actuel']['vent'])}" if v['actuel']['vent'] else ""), st_detail)]
            for per in (v["jour"], v["nuit"]):
                if per and per.get("texte"):
                    bloc.append(Paragraph(f"<i>{html.escape(per['nom'] or '')} :</i> {html.escape(per['texte'])}", st_detail))
            for a in v["alertes"]:
                bloc.append(Paragraph(f"<font color='#b00020'><b>ALERTE : {html.escape(a['titre'] or '')}</b></font>", st_detail))
            bloc.append(Spacer(1, 4))
            elements.append(KeepTogether(bloc))

    def pied(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#777777"))
        canvas.drawString(1.2 * cm, 0.6 * cm, "Données : Environnement et Changement climatique Canada (meteo.gc.ca)")
        canvas.drawRightString(landscape(letter)[0] - 1.2 * cm, 0.6 * cm, f"Page {doc_.page}")
        canvas.restoreState()

    doc.build(elements, onFirstPage=pied, onLaterPages=pied)


# ------------------------------- HTML --------------------------------------
def carte_html(v):
    e = html.escape
    if not v["ok"]:
        return f'<article class="carte"><h3>{e(v["nom"])}</h3><p class="muet">Données indisponibles</p></article>'
    j, n = v["jour"], v["nuit"]
    icone = (j or n or {}).get("icone")
    img = (f'<img src="{e(icone)}" alt="" width="45" height="38" loading="lazy" onerror="this.remove()">'
           if isinstance(icone, str) and icone.startswith("https://") else "")
    alertes = "".join(f'<a class="alerte {e(a.get("niveau") or "")}" href="{e(a.get("lien") or v["lien"])}" '
                      f'target="_blank" rel="noopener">⚠ {e(a.get("titre") or "Alerte")}</a>' for a in v["alertes"])

    def ligne(label, per, cle):
        if not per:
            return f'<div class="per"><span class="lbl">{label}</span><span class="muet">Période terminée</span></div>'
        pop = f' · {per["pop"]} % précip.' if per.get("pop") not in (None, "") else ""
        return (f'<div class="per"><span class="lbl">{label}</span><strong class="t">{deg(per.get(cle))}</strong>'
                f'<span>{e(per.get("conditions") or "—")}{pop}</span></div>')

    return f"""<article class="carte">
  <header><h3><a href="{e(v['lien'])}" target="_blank" rel="noopener">{e(v['nom'])}</a></h3>{img}</header>
  <p class="actuel">Maintenant : <strong>{deg(v['actuel']['temp'])}</strong> {e(v['actuel']['conditions'] or '')}</p>
  {ligne('Jour', j, 'max')}
  {ligne('Nuit', n, 'min')}
  <p class="muet">Normales {deg(v['normales']['max'])} / {deg(v['normales']['min'])} · Soleil {v['lever'] or '—'}–{v['coucher'] or '—'}</p>
  {alertes}
</article>"""


def generer_html(donnees, maintenant, archives):
    sections = "".join(
        f'<section><h2>{html.escape(prov)}</h2><div class="grille">{"".join(carte_html(v) for v in villes)}</div></section>'
        for prov, villes in donnees.items())
    liste_arch = "".join(
        f'<li><a href="archives/{a.name}" download>{date_longue(datetime.strptime(a.stem[6:], "%Y-%m-%d"))}</a></li>'
        for a in archives)
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Météo Québec–Ontario</title>
<meta name="description" content="Météo du jour des principales villes du Québec et de l'Ontario, mise à jour chaque matin à 5 h 30.">
<style>
:root {{ --fond:#f4f6f9; --carte:#fff; --texte:#1c2430; --muet:#5c6878; --accent:#1f4e79; --bord:#dde3ea; --chaud:#c0392b; --froid:#2e6da4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --fond:#12161c; --carte:#1b2129; --texte:#e6ebf1; --muet:#9aa6b5; --accent:#7fb0e0; --bord:#2b333e; --chaud:#ff8a7a; --froid:#8cc3ff; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; background:var(--fond); color:var(--texte); line-height:1.4; }}
.haut {{ background:var(--accent); color:#fff; padding:28px 16px 22px; }}
@media (prefers-color-scheme: dark) {{ .haut {{ background:#1d3550; }} }}
.haut > div, main {{ max-width:1200px; margin:0 auto; }}
h1 {{ margin:0 0 4px; font-size:clamp(1.5rem,4vw,2.2rem); }}
.haut p {{ margin:0; opacity:.9; }}
.bouton {{ display:inline-block; margin-top:16px; background:#fff; color:#1f4e79; font-weight:700; padding:12px 20px; border-radius:10px; text-decoration:none; }}
.bouton:hover {{ background:#e8f0f8; }}
main {{ padding:8px 16px 40px; }}
h2 {{ color:var(--accent); margin:28px 0 12px; }}
.grille {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(250px,1fr)); gap:12px; }}
.carte {{ background:var(--carte); border:1px solid var(--bord); border-radius:12px; padding:14px; }}
.carte header {{ display:flex; justify-content:space-between; align-items:center; gap:8px; }}
.carte h3 {{ margin:0; font-size:1.05rem; }}
.carte h3 a {{ color:inherit; text-decoration:none; }}
.carte h3 a:hover {{ text-decoration:underline; }}
.actuel {{ margin:6px 0 10px; color:var(--muet); font-size:.9rem; }}
.per {{ display:grid; grid-template-columns:42px 52px 1fr; align-items:baseline; gap:6px; padding:6px 0; border-top:1px solid var(--bord); font-size:.9rem; }}
.lbl {{ color:var(--muet); font-size:.8rem; text-transform:uppercase; letter-spacing:.03em; }}
.per:nth-of-type(1) .t {{ color:var(--chaud); }}
.t {{ font-size:1.35rem; }}
.per + .per .t {{ color:var(--froid); }}
.muet {{ color:var(--muet); font-size:.8rem; margin:8px 0 0; }}
.alerte {{ display:block; margin-top:8px; padding:6px 8px; border-radius:8px; font-size:.8rem; font-weight:600; background:#fff3cd; color:#7a5a00; text-decoration:none; }}
.alerte.orange {{ background:#ffe0c2; color:#8a3d00; }} .alerte.red {{ background:#ffd6d6; color:#8f0000; }}
.archives {{ margin-top:36px; }} .archives ul {{ columns:2 220px; padding-left:18px; }}
footer {{ color:var(--muet); font-size:.8rem; margin-top:32px; }}
footer a, .archives a {{ color:var(--accent); }}
</style>
</head>
<body>
<div class="haut"><div>
  <h1>Météo du {date_longue(maintenant)}</h1>
  <p>Principales villes du Québec et de l'Ontario · mis à jour à {maintenant.strftime('%H h %M')}</p>
  <a class="bouton" href="meteo-du-jour.pdf" download="meteo-{maintenant.strftime('%Y-%m-%d')}.pdf">⬇ Télécharger le PDF du jour</a>
</div></div>
<main>
{sections}
<section class="archives"><h2>PDF des jours précédents</h2><ul>{liste_arch}</ul></section>
<footer>Données : <a href="https://meteo.gc.ca" target="_blank" rel="noopener">Environnement et Changement climatique Canada (meteo.gc.ca)</a>.
Mise à jour automatique chaque matin vers 5 h 30 (heure de l'Est). « Jour » = maximum prévu aujourd'hui, « Nuit » = minimum prévu ce soir et cette nuit.</footer>
</main>
</body>
</html>
"""


# ----------------------------------------------------------------------------
def main():
    force = "--force" in sys.argv
    maintenant = datetime.now(FUSEAU)
    pdf_archive = ARCHIVES / f"meteo-{maintenant.strftime('%Y-%m-%d')}.pdf"

    if not force and maintenant.hour < 5:
        print(f"Il est {maintenant:%H:%M} : trop tôt, on attend l'exécution de 5 h 30.")
        return
    if not force and pdf_archive.exists():
        print("Le rapport d'aujourd'hui existe déjà, rien à faire.")
        return

    print("Téléchargement des prévisions…")
    donnees = recolter()
    nb_ok = sum(v["ok"] for villes in donnees.values() for v in villes)
    print(f"{nb_ok} villes récupérées sur {sum(len(v) for v in donnees.values())}.")
    if nb_ok == 0:
        sys.exit("Aucune donnée reçue d'Environnement Canada : on garde la version précédente.")

    ARCHIVES.mkdir(parents=True, exist_ok=True)
    generer_pdf(donnees, maintenant, pdf_archive)
    (DOCS / "meteo-du-jour.pdf").write_bytes(pdf_archive.read_bytes())

    # Ne garder que les 30 derniers jours
    limite = (maintenant - timedelta(days=JOURS_ARCHIVES)).strftime("%Y-%m-%d")
    for a in ARCHIVES.glob("meteo-*.pdf"):
        if a.stem[6:] < limite:
            a.unlink()
    archives = sorted(ARCHIVES.glob("meteo-*.pdf"), reverse=True)

    (DOCS / "index.html").write_text(generer_html(donnees, maintenant, archives), encoding="utf-8")
    (DOCS / "donnees.json").write_text(json.dumps({"genere": maintenant.isoformat(), "villes": donnees},
                                                  ensure_ascii=False, indent=1), encoding="utf-8")
    (DOCS / ".nojekyll").touch()
    print("Terminé : docs/index.html et docs/meteo-du-jour.pdf mis à jour.")


if __name__ == "__main__":
    main()
