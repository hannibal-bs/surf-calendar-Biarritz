# Calendrier Surf — Biarritz

Ce projet génère automatiquement un calendrier ICS de créneaux surf à partir du calendrier WebCalTides de Biarritz.

## Règles appliquées

- début du créneau = marée courante + 2h
- fin du créneau = marée suivante - 2h
- créneaux conservés uniquement entre 07:00 et 22:00
- durée minimale : 60 minutes
- les créneaux qui débordent sont tronqués ; ceux qui restent hors plage ou trop courts sont supprimés
- les événements `Sunrise` et `Sunset` du calendrier WebCalTides sont ignorés

Le calendrier source est :

```text
webcal://webcaltides.org/tides/T232e6c9.ics?units=metric
```

## Fichiers

```text
surf_calendar_generator.py
requirements.txt
.github/workflows/publish-surf-calendar.yml
```

## Déploiement GitHub Pages

1. Créer un repository GitHub, par exemple `surf-calendar-biarritz`.
2. Uploader tous les fichiers de ce dossier dans le repository, en conservant le dossier `.github/workflows`.
3. Aller dans `Settings` → `Pages`.
4. Sous `Build and deployment`, choisir `Source: GitHub Actions`.
5. Aller dans `Actions` → `Publish surf calendar` → `Run workflow`.
6. Une fois le workflow terminé, le calendrier sera disponible à une URL du type :

```text
https://VOTRE-NOM-UTILISATEUR.github.io/surf-calendar-biarritz/surf-biarritz.ics
```

## Abonnement Outlook

Dans Outlook Web :

1. Calendrier
2. Ajouter un calendrier
3. S’abonner à partir du web
4. Coller l’URL publique du fichier `surf-biarritz.ics`
5. Nommer le calendrier, par exemple `Surf — Biarritz`

## Mise à jour automatique

Le workflow GitHub Actions tourne tous les jours à 05:17 UTC et republie le fichier ICS.

Attention : Outlook peut mettre plusieurs heures, parfois plus de 24h, à rafraîchir un abonnement ICS.

## Test local

```bash
python -m pip install -r requirements.txt
python surf_calendar_generator.py --source "webcal://webcaltides.org/tides/T232e6c9.ics?units=metric" --output surf-biarritz.ics --timezone Europe/Paris --day-start-hour 7 --day-end-hour 22 --min-duration-minutes 60 --preview 12
```
