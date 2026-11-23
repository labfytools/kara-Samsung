"""Normalisation des paroles et vocabulaire des chansons."""
from collections import Counter
import re
from unicodedata import combining, normalize


SPELLINGS = {
    "ouech": "wesh", "ouaiche": "wesh", "wech": "wesh", "weche": "wesh",
    "charro": "charo", "sharo": "charo", "frero": "frerot",
    "mollah": "moula", "moulah": "moula", "mullah": "moula",
    "seumme": "seum", "tchek": "tcheck", "biffe": "biff", "blaze": "blase",
}
CONTRACTIONS = {"chuis": ("je", "suis"), "chui": ("je", "suis"), "jsuis": ("je", "suis"),
                "jsais": ("je", "sais"), "jai": ("j", "ai")}
ARGOT = frozenset("""
    wesh charo moula reuf meuf seum chelou daron daronne frerot biff
    tema tcheck tchop teuf keuf beuh sheitan sah cote pilon zinzin
    dinguerie boloss bolos sonper cimer relou chanme
""".split())
FRENCH = frozenset("""
    je j suis sais tu t tes mes votre vous nous eux leur leurs
    mon ma moi toi les des dans une avec pour pas mais est cest c qui
    fait faut quand comme puis quoi pourquoi beaucoup toujours rien
""".split())


# Indices volontairement distinctifs : les articles communs (un, me, no,
# que...) ne suffisent pas à imposer une langue à Whisper.
ENGLISH = frozenset("""
    the and you your youre yours i im ive my mine we were they their
    he she his her it its this that these those with from where when
    what why who how dont doesnt cant wont would could should gonna
    wanna love baby heart tonight night never always forever feel
    feeling know knows knew think thought want need make made take
    give gave tell told say said see seen look looking come came
""".split())
SPANISH = frozenset("""
    estoy estas estamos eres somos soy tienes tengo tiene tenemos
    quiero quieres queremos puede puedo puedes porque aunque mientras
    conmigo contigo nosotros vosotros ustedes ellos ellas nuestro
    nuestra nuestros nuestras corazon corazoncito noche noches siempre
    nunca jamas amor besos beso besame quieres dime dame dejame
    vamos voy vas viene vuelvo sabes sabe sabes mirame llorar
    llora bailar bailando vida quiero querer sueno suenos manana
    ahora ayer entonces despues hasta donde cuando como estas
""".split())
LANGUAGE_MARKERS = {"fr": FRENCH | ARGOT, "en": ENGLISH, "es": SPANISH}
LANGUAGE_LABELS = {"fr": "Vocabulaire de la chanson.",
                   "en": "Song vocabulary.", "es": "Vocabulario de la canción."}


def detect_language(tokens):
    """Return a confident lyrics language or auto-detection for ambiguous text.

    A sufficiently long song provides several different language clues. Shared
    words do not vote, and a tie/mixed-language song remains in Whisper auto.
    """
    counts = Counter(tokens)
    exclusive = {lang: markers - set().union(*(other for code, other in LANGUAGE_MARKERS.items()
                                               if code != lang))
                 for lang, markers in LANGUAGE_MARKERS.items()}
    scored = {lang: (sum(counts[token] for token in unique),
                     sum(token in counts for token in unique))
              for lang, unique in exclusive.items()}
    ranking = sorted(scored, key=lambda lang: scored[lang][0], reverse=True)
    winner, other = ranking[:2]
    hits, distinct = scored[winner]
    if hits >= 4 and distinct >= 2 and hits >= scored[other][0] * 1.5 + 2:
        return winner
    return ""


def words(text):
    text = re.sub(r"\[[^\]]*\]", " ", text.casefold())
    text = "".join(char for char in normalize("NFKD", text) if not combining(char))
    tokens = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    result = []
    for index, token in enumerate(tokens):
        if token == "j" and index + 1 < len(tokens) and tokens[index + 1] in {"suis", "sais"}:
            token = "je"
        result.extend(CONTRACTIONS.get(token, (SPELLINGS.get(token, token),)))
    return result


IGNORED_HINTS = FRENCH | frozenset("""
    le la de du et un en ne se sa son sur est ait ses cet ces elle ils y
    the and that this with for from you your have was were will are not
    you could would what how she her his they them him but more mine ours
    que eso esa este esta los las una por para mis tus del con todo como
""".split())
FORM = re.compile(r"[^\W\d_]+(?:['’\-][^\W\d_]+)*")


class LyricVocabulary:
    def __init__(self, lines):
        self.lines = tuple(lines)
        self.tokens = tuple(tuple(words(line)) for line in self.lines)
        flat = [token for line in self.tokens for token in line]
        self.language = detect_language(flat)
        self.forms = {}
        self.by_line = []
        for line in self.lines:
            choices = set()
            for form in FORM.findall(re.sub(r"\[[^\]]*\]", " ", normalize("NFC", line))):
                key = tuple(words(form))
                if (key and 3 <= len(form) <= 26
                        and any(token not in IGNORED_HINTS and len(token) >= 3 for token in key)):
                    self.forms.setdefault(key, form)
                    choices.add(key)
            self.by_line.append(choices)
        self.occurrences = Counter(key for line in self.by_line for key in line)
        self.refrain = sorted(self.forms, key=lambda key: (not self.is_slang(key), -self.occurrences[key], key))

    @staticmethod
    def is_slang(key):
        return any(token in ARGOT for token in key)

    def prompt(self, first, last):
        local = set().union(*self.by_line[first:last + 1])
        nearby = set().union(*self.by_line[max(0, first - 1):last + 2]) - local
        rank = lambda key: (not self.is_slang(key), self.occurrences[key], key)
        keys = sorted(local, key=rank)[:9]
        keys.extend(sorted(nearby, key=rank)[:4])
        keys.extend(key for key in self.refrain[:6] if key not in keys)
        hints = ", ".join(sorted((self.forms[key] for key in keys[:16]), key=str.casefold))
        if not hints:
            return ""
        label = LANGUAGE_LABELS.get(self.language, "Song vocabulary.")
        return f"{label} {hints}."
