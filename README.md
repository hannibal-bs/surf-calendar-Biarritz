# Calendrier Surf — Côte des Basques, Biarritz

Ce projet génère automatiquement un calendrier ICS de **fenêtres de surf potentiellement favorables** à partir des marées WebCalTides de Biarritz.

## Règles appliquées — calibration retenue au 20 octobre 2026

- **Plage de hauteur : de 1,65 à 2,40 m**, dans le référentiel WebCalTides/TICON (station `T232e6c9`).
- **Hauteur optimale présumée : 2,10 m**, informative uniquement : ce n'est pas un filtre supplémentaire.
- Pour chaque portion de marée entre deux extrêmes consécutifs, déterminer les heures où la hauteur prévue se situe dans la plage 1,65–2,40 m.
- Estimer les franchissements par **interpolation cosinus** entre les hauteurs et horaires des pleines/basses mers. C'est une approximation et non une courbe réelle de marée.
- Retenir la **montante et la descendante** et calculer les **durées variables** de chaque intervalle favorable, plutôt que des fenêtres fixes de 90 minutes.
- **Fusionner les portions contiguës**, par exemple lorsqu'une pleine mer entre 1,65 et 2,40 m permet un seul créneau continu autour de la pleine mer.
- Si une pleine mer reste sous 1,65 m, ne pas créer de créneau de repli.
- Garder seulement les portions de créneau entre **07:00 et 22:00** ; découper aux limites quotidiennes.
- Écarter après fusion et découpe tout créneau de **moins de 60 minutes**.
- Ignorer les levers/couchers du soleil et interrompre la génération en cas de hauteurs de marée manquantes.

**Attention :** un créneau indique seulement une plage de **niveau d'eau théoriquement favorable**. Le générateur ne tient compte ni du vent, ni de la houle, ni de la surcote, ni de la sécurité d'entrée ou de sortie devant les rochers. **2,40 m n'est pas un seuil d'accès sûr.** Les hauteurs TICON ne sont pas directement interchangeables avec celles du SHOM, de Marée.info ou de La Bouée. Cette calibration est conservée tant que les observations des sessions futures ne justifient pas une modification.

## Infrastructure (inchangée)

- Source : `webcal://webcaltides.org/tides/T232e6c9.ics?units=metric`
- Générateur : `surf_calendar_generator.py`
- Publication : `.github/workflows/publish-surf-calendar.yml`
- Fichier public : `surf-biarritz.ics` via GitHub Pages.
- Publication automatique tous les jours à **05:17 UTC**.
- Consommation par abonnement au fichier ICS dans Outlook. La synchronisation Outlook peut prendre plusieurs heures.

## Test local

```bash
python -m pip install -r requirements.txt
python surf_calendar_generator.py \
  --source "webcal://webcaltides.org/tides/T232e6c9.ics?units=metric" \
  --output surf-biarritz.ics \
  --timezone Europe/Paris \
  --lower-height-m 1.65 \
  --upper-height-m 2.40 \
  --optimal-height-m 2.10 \
  --day-start-hour 7 \
  --day-end-hour 22 \
  --min-duration-minutes 60 \
  --preview 12
```

## Exemples

- Le 15 octobre à la descendante, le passage théorique de 2,40 m à 1,65 m donne un créneau d'environ **09h53 à 11h11**.
- Si une pleine mer est à 2,20 m, les phases montante et descendante peuvent se rejoindre en un **créneau continu** autour de cette pleine mer.
- Si la pleine mer atteint seulement 1,60 m, **aucun créneau de repli** n'est généré.

La plage basse/haute, le repère optimal et la durée minimale restent configurables sans modification de l'infrastructure.
