# Ajax DoorBell pour Home Assistant

[English](README.md)

Un interphone local pour l'[Ajax DoorBell](https://ajax.systems/products/doorbell/) :
vidéo live quasi sans latence, écouter et parler au visiteur, sonnerie en moins d'une
seconde, état d'appel partagé entre tous les écrans, carte interphone et notification
avec un bouton **Décrocher**.

Tout reste sur ton réseau. L'intégration parle à la DoorBell par son interface
officielle ONVIF et RTSP : ni cloud Ajax, ni mot de passe du compte Ajax.

## Fonctions

- **Vidéo live** par le go2rtc intégré à Home Assistant, en WebRTC. Le flux H.264
  est copié tel quel, jamais réencodé : environ 0,3 s de bout en bout sur le LAN.
- **Écouter** : l'audio G.722 de la DoorBell arrive dans le navigateur en Opus.
- **Parler** : le micro du navigateur repart vers le haut-parleur de la DoorBell par
  le canal de retour audio ONVIF (backchannel). L'intégration vérifie à la
  configuration que la DoorBell en propose un ; avec Home Assistant 2026.11, la
  caméra déclare alors l'audio bidirectionnel et le bouton **Parler** de Home
  Assistant (aperçu Labs) fonctionne aussi.
- **Sonnerie en moins d'une seconde** grâce à l'événement ONVIF local de la DoorBell
  (`RingDetector`), sous forme d'entité `event` de classe `doorbell`.
- **État d'appel partagé par tous les écrans** : inactif, sonne, en cours. Décrocher
  sur un écran arrête la sonnerie partout ; les sonneries sans réponse comptent comme
  appels manqués.
- **Détections** : mouvement, personne, véhicule et animal, par l'IA de la DoorBell.
- **Carte interphone** `custom:ajax-doorbell-card` et **blueprint de notification**
  (Décrocher, Voir, Ignorer, Ouvrir la porte).
- Découverte DHCP, réauthentification, reconfiguration, diagnostics, français et
  anglais.

## Prérequis

- Ajax DoorBell en firmware 2.377 ou plus récent (prise en charge ONVIF).
- Home Assistant 2026.10 ou plus récent. La 2026.11 ajoute le bouton Parler natif.
- HTTPS pour utiliser le micro dans le navigateur ou l'app compagnon.

## 1. Préparer la DoorBell dans l'app Ajax

1. **Appareils → DoorBell → Paramètres → Service → Connexion via ONVIF** : active
   **Autoriser l'intégration ONVIF** avec les réglages par défaut (port ONVIF 8080,
   RTSP 8554).
2. **Utilisateurs ONVIF** : ajoute un utilisateur pour Home Assistant (par exemple
   `homeassistant`) avec le rôle **Opérateur** et un long mot de passe. Ajax autorise
   cinq utilisateurs ONVIF par appareil.
3. Réglages de flux conseillés : un GOP égal au nombre d'images par seconde (une
   image clé par seconde) pour un démarrage instantané, et 2 à 3 Mbit/s sur le flux
   principal, car la DoorBell est en Wi-Fi 2,4 GHz.
4. Réserve une adresse fixe à la DoorBell dans ton routeur (bail DHCP).

## 2. Installer l'intégration

Ajoute ce dépôt comme dépôt personnalisé (Marketplace ou HACS, catégorie
**Intégration**), installe **Ajax DoorBell**, puis redémarre Home Assistant. Ou copie
`custom_components/ajax_doorbell` dans ton dossier `config/custom_components/`.

Home Assistant découvre la DoorBell sur ton réseau. Sinon, ajoute-la depuis
**Paramètres → Appareils et services → Ajouter une intégration → Ajax DoorBell**,
avec son adresse IP et l'utilisateur ONVIF.

## Entités

| Entité | Description |
| --- | --- |
| `camera.<sonnette>` | Flux principal (jusqu'à 2688×1520), WebRTC par go2rtc, audio bidirectionnel |
| `camera.<sonnette>_flux_secondaire` | Flux léger pour les petites vignettes (désactivé par défaut) |
| `event.<sonnette>_sonnette` | Émet `ring` à chaque appui |
| `binary_sensor.<sonnette>_appel_entrant` | Allumé pendant la sonnerie |
| `sensor.<sonnette>_appel` | `idle`, `ringing` ou `in_call`, avec `answered_by` et les heures |
| `sensor.<sonnette>_derniere_sonnerie` | Heure du dernier appui |
| `sensor.<sonnette>_appels_manques` | Sonneries sans réponse depuis la dernière remise à zéro |
| `binary_sensor.<sonnette>_mouvement`, `_personne`, `_vehicule`, `_animal` | Détections |
| `binary_sensor.<sonnette>_flux_d_evenements` | Santé de l'abonnement aux événements ONVIF (diagnostic) |

Les identifiants exacts dépendent du nom de l'appareil et de la langue de Home
Assistant au moment de l'installation.

## Services

| Service | Effet |
| --- | --- |
| `ajax_doorbell.answer` | Marque l'appel comme décroché (`answered_by` facultatif) ; l'appeler à nouveau garde l'appel actif |
| `ajax_doorbell.hang_up` | Termine l'appel |
| `ajax_doorbell.dismiss` | Arrête la sonnerie sans décrocher |
| `ajax_doorbell.clear_missed` | Remet à zéro le compteur d'appels manqués |

Cible une entité ou l'appareil DoorBell ; sans cible, le service agit sur toutes les
DoorBell.

## Événements

`ajax_doorbell_call` est émis à chaque changement d'appel, avec `type` (`ring`,
`answered`, `ended`, `missed`, `dismissed`, `missed_cleared`), `state`, `device_id` et
`config_entry_id`, plus `answered_by`, `after_ring`, `during_call` ou `reason` selon
le cas.

## Carte interphone

1. Copie `card/ajax-doorbell-card.js` dans ton dossier `config/www/`.
2. **Paramètres → Tableaux de bord → ⋮ → Ressources → Ajouter une ressource** :
   `/local/ajax-doorbell-card.js`, type **Module JavaScript**.
3. Ajoute la carte à une vue (une vue « Interphone » dédiée est idéale) :

```yaml
type: custom:ajax-doorbell-card
camera: camera.ajax_doorbell             # obligatoire
call_entity: sensor.ajax_doorbell_appel  # facultatif, trouvé sur l'appareil de la caméra
name: Entrée                             # par défaut : le nom de la caméra
answered_by: Tablette de l'entrée        # par défaut : "<utilisateur> (téléphone|tablette|ordinateur)"
two_way_audio: true                      # true force la parole, false l'écoute seule
push_to_talk: false                      # maintenir le bouton pour parler
preview_on_ring: true                    # lance la vidéo en muet dès la sonnerie
ring_sound: false                        # sonnerie dans le navigateur
fullscreen_on_ring: false
show_stats: false                        # RTT, gigue, résolution, codecs
auto_retry: true
aspect_ratio: "16 / 9"
```

Quand ça sonne, la carte affiche un écran d'appel entrant avec **Décrocher**,
**Voir** et **Ignorer**. **Voir** puis **Décrocher** garde la même connexion : seul
le micro s'ajoute. Décrocher sur un écran affiche « Décroché sur … » sur les autres.
Ouvrir la vue avec `?ajax_answer=1` (ou `?ajax_answer=camera.ajax_doorbell` s'il y a
plusieurs sonnettes) décroche aussitôt : c'est ce qu'utilise le bouton de la
notification.

## Notification avec bouton Décrocher

Importe le blueprint
[`blueprints/automation/ajax_doorbell/doorbell_notify.yaml`](blueprints/automation/ajax_doorbell/doorbell_notify.yaml)
(**Paramètres → Automatisations → Blueprints → Importer**). Quand quelqu'un sonne,
les téléphones choisis reçoivent l'image de la porte avec **Décrocher**, **Voir** et
**Ignorer**, plus **Ouvrir la porte** si tu choisis une serrure. **Décrocher** ouvre
ta vue d'interphone, dont la carte prend l'appel aussitôt. La notification disparaît
de tous les téléphones dès que quelqu'un répond ; un appel manqué peut laisser une
notification à la place. Sur Android, elle peut aussi ouvrir la vue de l'interphone
toute seule, comme un appel entrant.

## Dépannage

- **Pas de micro** : la page doit être servie en HTTPS (ou `http://localhost`).
- **Pas de bouton Parler ou pas de son à la porte** : regarde l'attribut
  `two_way_audio` du capteur d'appel et le backchannel dans les diagnostics. Parler
  demande que la DoorBell propose un backchannel en G.711 ou Opus.
- **Latence plus élevée** : ne règle pas d'orientation de caméra dans Home Assistant
  (elle force le réencodage de la vidéo) et évite les lecteurs HLS.
- **Accès à distance en 4G** : le média passe directement en WebRTC. Derrière un NAT
  mobile strict, déclare un serveur TURN sous `web_rtc: ice_servers:` dans
  `configuration.yaml`.
- **Flux d'événements hors ligne** : l'utilisateur ONVIF n'a peut-être pas le droit de
  s'abonner aux événements : donne-lui le rôle Opérateur.

## Fonctionnement

```
DoorBell ── RTSP 8554 (H.264, G.722, backchannel) ──► go2rtc ── WebRTC ──► navigateur
DoorBell ── ONVIF 8080 (événements PullPoint) ──► ajax_doorbell ──► entités, appel, notification
```

L'app Ajax continue de fonctionner comme avant : elle utilise son propre canal
chiffré, l'accès ONVIF est une porte locale indépendante.

## Confidentialité et sécurité

Le compte ONVIF contourne le cloud Ajax : traite-le comme une clé. Un utilisateur
dédié, un long mot de passe, et aucun port de la DoorBell ouvert vers Internet. Les
identifiants restent dans la configuration de Home Assistant ; les diagnostics et les
journaux ne les contiennent jamais.

## Licence

MIT, voir [LICENSE](LICENSE). Sans lien avec Ajax Systems.
