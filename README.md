# UBGME 1.0 — Ultra Basic Generic Mod Enabler

UBGME (*Ultra Basic Generic Mod Enabler*) est un gestionnaire de mods Linux en Python/Tkinter largement inspiré de l'antique JSGME.
Il permet d'activer simplement des mods en les déposants dans un répertoire prévu à cet effet et en calquant leur arborescence dans ce répertoire sur celle qu'ils occuperaient dans le dossier du jeu.

> **Prudence :** ce programme est totalement vibe-codé par IA. Les tests de base ont été fait sur un véritable répertoire de jeu par un humain. Cependant, je ne suis ni développeur, ni expert coding/IA, donc vous utilisez UBGME en toute connaissance de cause. 

## Pour utiliser UBGME

1. **Placez `ubgme.py` à la racine du jeu,**
2. **créez le répertoire `MODS/`,**
3. **puis placez chaque mod dans un sous-dossier dont les chemins reproduisent ceux du jeu** :

```text
racine-du-jeu/
├── ubgme.py
├── MODS/
│   └── Exemple/
│       ├── readme
│       └── data/fichier.dat
└── data/fichier.dat
```

4. **Lancez `python3 ubgme.py`**; afficher la version avec `python3 ubgme.py --version`. Pour les essais, la racine peut être indiquée par `python3 ubgme.py --game-root /chemin/vers/jeu-test`. Tkinter doit être installé et disponible dans Python; suivant la distribution, le paquet système `python3-tk` peut être nécessaire.

Un fichier `readme.txt` à la racine du mod (casse ignorée) est affiché comme description et jamais déployé. `readme` sans extension et `readme.md` sont traités comme des fichiers ordinaires du mod. Les chemins/fichiers dont un composant commence par `.` sont ignorés. Un mod sans fichier déployable apparaît dans la liste, mais ne peut pas être activé.

## Limites connues de la v1.0

- Le moteur gère les sauvegardes immuables, transactions, rollback/récupération et collisions. Au lancement, `state.json` est validé structurellement sans rescanner/hacher les mods actifs; les empreintes des fichiers et sauvegardes sont vérifiées juste avant l’opération qui les touche. Une modification externe sera donc signalée à l’opération, et non à l’ouverture.
- Les répertoires créés par UBGME sont suivis par propriétaire; à la désactivation, seuls les dossiers enregistrés et vides sont retirés. Les dossiers préexistants restent intacts; ceux qui contiennent encore des éléments sont conservés avec un avertissement consolidé indiquant le répertoire et les fichiers restants. L’utilisateur a validé ce comportement dans la GUI. Les répertoires créés par des activations antérieures à ce suivi ne sont pas supprimés automatiquement, car leur origine ne peut pas être établie sûrement.
- L’inventaire, l’activation et la désactivation sont exécutés hors du thread Tk. L’utilisateur confirme la réactivité et la clarté de la progression; il a également validé le message consolidé des répertoires et fichiers restants.
- Les deux listes GUI gèrent Ctrl+clic, Maj+clic et Maj+flèches avec surlignage bleu et indication des noms/nombre sélectionnés. La désactivation groupée retire d’abord les mods de priorité supérieure.
- `MODS/` et `.modmanager/` sont créés à l’initialisation si absents. L’application ne télécharge ni n’extrait de mods.
