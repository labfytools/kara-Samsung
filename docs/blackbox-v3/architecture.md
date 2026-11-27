# BlackBox V3

Parole possède l’enregistreur. La nouvelle capture consigne les positions de Strawberry, les lignes du LRC, les déplacements et les réponses de la télévision. Le journal JSONL est suivi d’un manifeste intègre et d’un bilan JSON. Les positions de lecture sont exprimées en secondes. Les événements sont ordonnés sur l’horloge monotone de l’hôte, alors que l’horloge UTC identifie les sessions.

Le serveur associe une réponse TV à une révision déjà publiée. Il garde un nombre borné de révisions en mémoire. Une révision inconnue ne prouve aucune position affichée. Le délai calculé relie les observations logicielles du serveur et de l’affichage.

Les anciennes captures peuvent contenir du PCM, des identifiants de tour, des événements et des rapports émis par les anciens outils. La vérification et l’extraction de ces fichiers lisent le format historique. Les publications de diagnostics sont limitées à l’état utilisateur privé. La branche `archive/kara-multijoueur-2.1.0` conserve l’analyse des anciens parcours.
