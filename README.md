# Calendrier Surf — Côte des Basques, Biarritz

Ce projet génère automatiquement un calendrier ICS de **créneaux de surf indicatifs** à partir des prédictions de marées WebCalTides de Biarritz.

## Règles appliquées

- **Hauteur cible provisoire : 2,90 m**, dans le référentiel propre à la source WebCalTides (à valider par observation sur le spot).
- Pour chaque intervalle entre marée haute et marée basse, trouver le passage théorique à cette hauteur.
- Générer un créneau de **90 minutes centré sur le passage** (45 minutes avant et 45 minutes après).
- Conserver **marée montante et marée descendante**.
- Calculer le passage par interpolation cosinus entre les hauteurs extrêmes et leurs horaires (approximation, **pas une courbe de marée exacte**).
- **Lorsque la pleine mer est inférieure ou égale à 2,90 m**, créer **un seul créneau de repli** de 90 minutes centré sur la pleine mer (pas de doublon montante/descendante). Ce créneau est marqué « repli (pleine mer) » dans Outlook.
- Lorsque la pleine mer est supérieure à 2,90 m, conserver les créneaux normaux centrés sur les passages à 2,90 m.
- Si un intervalle de marée ne traverse pas la hauteur cible, ne pas inventer de passage : seul un éventuel repli autour d'une pleine mer basse est proposé.
- Garder les créneaux seulement entre **07:00 et 22:00** ; ceux qui débordent sont tronqués.
- Supprimer un créneau tronqué s'il dure moins de **60 minutes**.
- Ignorer les événements `Sunrise` et `Sunset` dans la source.
- Ne pas publier de nouveau calendrier si les hauteurs sont absentes/inexploitables.

**Important :** les heures sont des estimations purement liées à la marée. Ni la qualité réelle des vagues, ni la houle, ni la sécurité d'accès/sortie auprès des rochers ne sont évaluées. Le niveau de 2,90 m reste à calibrer **dans le référentiel de WebCalTides** ; ne pas supposer que toutes les sources utilisent le même zéro vertical.

## Infrastructure (inchangée)

- Source : `webcal://webcaltides.org/tides/T232e6c9.ics?units=metric`
- Générateur : `surf_calendar_generator.py`
- Publication : `.github/workflows/publish-surf-calendar.yml`
- Fichier public : `surf-biarritz.ics` via GitHub Pages.
- Publication automatique : tous les jours à **05:17 UTC**.
- Consommation : abonnement au fichier ICS dans Outlook. La synchronisation Outlook peut prendre plusieurs heures.

## Test local

```bash
python -m pip install -r requirements.txt
python surf_calendar_generator.py \
  --source "webcal://webcaltides.org/tides/T232e6c9.ics?units=metric" \
  --output surf-biarritz.ics \
  --timezone Europe/Paris \
  --target-height-m 2.90 \
  --window-duration-minutes 90 \
  --day-start-hour 7 \
  --day-end-hour 22 \
  --min-duration-minutes 60 \
  --preview 12
```

## Exemple de créneau de repli

Si une pleine mer atteint 2,65 m à 14:00, l'événement devient « Surf — Côte des Basques — repli (pleine mer) », de 13:15 à 14:45.

Un créneau de repli n'est **pas une promesse de bonnes vagues** ni une preuve de sécurité. Il n'existe pour l'instant aucun seuil de hauteur minimale de qualité ni de limite maximale d'accès sécurisée.

La hauteur cible et la durée sont configurables par les paramètres de commande sans changement d'infrastructure.