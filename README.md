# UBGME 1.0 — Ultra Basic Generic Mod Enabler

UBGME (*Ultra Basic Generic Mod Enabler*) est un gestionnaire de mods Linux en Python/Tkinter. La version fonctionnelle 1.0 garde le script applicatif dans `ubgme.py`; les tests sont séparés.

> **Prudence :** même si la v1.0 a été validée sur un arbre de test, ne l’utilise pas encore sur ton installation de jeu principale. Continue avec une copie jetable et une sauvegarde indépendante.

## Lancer

Placez `ubgme.py` à la racine d’une **copie de test** du jeu, créez `MODS/`, puis placez chaque mod dans un sous-dossier dont les chemins reproduisent ceux du jeu :

```text
racine-du-jeu/
├── ubgme.py
├── MODS/
│   └── Exemple/
│       ├── readme
│       └── data/fichier.dat
└── data/fichier.dat
```

Lancer `python3 ubgme.py`; afficher la version avec `python3 ubgme.py --version`. Pour les essais, la racine peut être indiquée par `python3 ubgme.py --game-root /chemin/vers/jeu-test`. Tkinter doit être installé et disponible dans Python; suivant la distribution, le paquet système `python3-tk` peut être nécessaire.

Un fichier `readme.txt` à la racine du mod (casse ignorée) est affiché comme description et jamais déployé. `readme` sans extension et `readme.md` sont traités comme des fichiers ordinaires du mod. Les chemins/fichiers dont un composant commence par `.` sont ignorés. Un mod sans fichier déployable apparaît dans la liste, mais ne peut pas être activé.

## Tests

Depuis ce répertoire :

```text
python3 -m unittest -v test_ubgme
```

Les tests créent leurs propres répertoires temporaires et n’utilisent aucun jeu réel.

## Limites connues de la v1.0

- Le moteur gère les sauvegardes immuables, transactions, rollback/récupération et collisions. Au lancement, `state.json` est validé structurellement sans rescanner/hacher les mods actifs; les empreintes des fichiers et sauvegardes sont vérifiées juste avant l’opération qui les touche. Une modification externe sera donc signalée à l’opération, et non à l’ouverture.
- Les répertoires créés par UBGME sont suivis par propriétaire; à la désactivation, seuls les dossiers enregistrés et vides sont retirés. Les dossiers préexistants restent intacts; ceux qui contiennent encore des éléments sont conservés avec un avertissement consolidé indiquant le répertoire et les fichiers restants. L’utilisateur a validé ce comportement dans la GUI. Les répertoires créés par des activations antérieures à ce suivi ne sont pas supprimés automatiquement, car leur origine ne peut pas être établie sûrement.
- L’inventaire, l’activation et la désactivation sont exécutés hors du thread Tk. L’utilisateur confirme la réactivité et la clarté de la progression; il a également validé le message consolidé des répertoires et fichiers restants.
- Les deux listes GUI gèrent Ctrl+clic, Maj+clic et Maj+flèches avec surlignage bleu et indication des noms/nombre sélectionnés. La désactivation groupée retire d’abord les mods de priorité supérieure; ces comportements avaient été validés manuellement par l’utilisateur.
- 34 tests automatisés passent; ils couvrent notamment les répertoires préexistants/créés, les avertissements, la reprise transactionnelle et les callbacks de progression. La baseline synthétique de 40 mods/4 000 fichiers actifs démarre en 0,093 s dans le conteneur agent, contre 2,280 s avant l’optimisation; les performances du poste utilisateur restent à comparer.
- Les erreurs et arrêts de processus sont simulés en tests; une panne électrique, un défaut matériel ou une coupure au niveau du système de fichiers n’ont pas été testés. Faire une sauvegarde indépendante avant tout essai sur un vrai jeu.
- La licence MIT a été retenue comme intention en cas de publication; le projet n’est pas encore doté d’un fichier `LICENSE` avec titulaire et année.
- `MODS/` et `.modmanager/` sont créés à l’initialisation si absents. L’application ne télécharge ni n’extrait de mods.
