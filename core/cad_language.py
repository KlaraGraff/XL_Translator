"""Decide whether one CAD text is already in the target language.

图纸文字大多是短标签（「PLAN DE MASSE」「COUPE A-A」「Door」），以前只看文字
用的是哪种书写系统：目标语言是英文时，凡是只有拉丁字母、没有汉字的文字一律
算「已是英文」。法文图纸要翻成英文，扫描结果就是 0 处待翻译——法文、西班牙
文、德文和英文用的是同一套字母，光看字母分不出来。

现在分三种结论：

* ``TARGET``：有把握已经是目标语言，扫描和翻译都跳过；
* ``FOREIGN``：有把握不是目标语言，照常翻译；
* ``UNSURE``：程序判断不了（多半是没有虚词、没有重音字母的短标签），交给
  模型：翻译时让模型同时回报原文的语种代码，回报的就是目标语言时保留原文。

拉丁字母语言之间的判断只用两类证据：常见虚词和图纸常用词（各语言独有的
那些，「PLAN」「NOTE」「TYPE」这类多种语言通用的词不收），以及只有某几种
语言才用的重音字母。证据不够就是 ``UNSURE``，宁可多问一次模型，也不能把
外文当成目标语言漏掉。
"""

from __future__ import annotations

import re
import unicodedata

TARGET = "target"
FOREIGN = "foreign"
UNSURE = "unsure"

# 拉丁字母书写的目标语言。这些语言之间不能靠书写系统区分。
LATIN_TARGET_LANGS = frozenset({
    "en", "fr", "de", "es", "it", "pt", "nl", "sv", "da", "no", "nb", "nn", "fi",
    "pl", "cs", "sk", "sl", "hu", "ro", "tr", "vi", "id", "ms", "ca", "hr", "sw",
})

_CJK_TARGET_PATTERNS = {
    "zh": r"[㐀-鿿]",
    "ja": r"[぀-ヿ㐀-鿿]",
    "ko": r"[가-힯]",
}
_SCRIPT_TARGET_PATTERNS = {
    "ar": r"[؀-ۿ]", "fa": r"[؀-ۿ]", "ur": r"[؀-ۿ]",
    "he": r"[֐-׿]", "th": r"[฀-๿]", "km": r"[ក-៿]",
    "hi": r"[ऀ-ॿ]", "bn": r"[ঀ-৿]", "ta": r"[஀-௿]",
    "te": r"[ఀ-౿]", "ml": r"[ഀ-ൿ]", "kn": r"[ಀ-೿]",
    "gu": r"[઀-૿]", "pa": r"[਀-੿]", "my": r"[က-႟]",
    "si": r"[඀-෿]", "lo": r"[຀-໿]", "am": r"[ሀ-፿]",
    "ru": r"[Ѐ-ӿ]", "uk": r"[Ѐ-ӿ]", "bg": r"[Ѐ-ӿ]",
    "sr": r"[Ѐ-ӿ]", "kk": r"[Ѐ-ӿ]", "mn": r"[Ѐ-ӿ]",
    "el": r"[Ͱ-Ͽ]",
}

# 图纸里大量出现 M10、Q235、GB/T、DN50、PVC 这类型号/规范代号。它们由拉丁字母
# 组成，但不是"外文"，判断语言时不能把它们当成英文单词。
_LATIN_WORD_RE = re.compile(r"(?<![A-Za-zÀ-ÿ0-9])[A-Za-zÀ-ÿ]+(?![A-Za-zÀ-ÿ0-9])")
_LATIN_LETTER_RE = re.compile(r"[A-Za-zÀ-ÿ]")
NON_LATIN_SCRIPT_RE = re.compile(
    r"[぀-ヿ㐀-鿿가-힯Ѐ-ӿ֐-ۿ฀-໿]"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)

# 各语言独有的常见词：虚词 + 图纸常用词。比较时去掉重音、统一小写，所以大写
# 标签里省掉重音（FENETRE、ECHELLE）也能认出来。多种语言共用的词（plan、
# detail、note、type、section 在英法文里都有）一律不收。
_LEXICON: dict[str, frozenset[str]] = {
    "en": frozenset("""
        the of and to for with from on at by this that is are be or not all any
        floor floors wall walls door doors window windows roof stair stairs steel
        concrete beam beams column columns slab foundation layout drawing drawings
        scale level room rooms building water pipe pipes supply typical reinforcement width height length thickness
        bottom top side front back north south east west inside outside sheet
        project main new existing area areas road kitchen bedroom bathroom toilet
        office storage ground first second third upper lower ceiling lighting
        power socket switch fire boundary fence gate tank pump room
        see refer schedule legend unless otherwise noted shown
        drawn checked approved key
        between above below under over each per only shall must should
        rebar embedded earthworks formwork
    """.split()),
    "fr": frozenset("""
        le la les de des du et au aux pour avec sur sous par un une dans est sont
        cette ces aux voir selon suivant sauf contraire
        coupe niveau mur murs poteau poteaux poutre poutres dalle dalles escalier
        escaliers porte portes fenetre fenetres toiture beton acier echelle chambre
        salle cuisine batiment batiments plancher etage etages rez chaussee
        longueur largeur hauteur epaisseur eau tuyau evacuation assainissement
        electricite eclairage prise nord sud ouest projet fondation fondations
        semelle semelles longrine longrines chainage charpente toit couverture
        plafond sol cloture portail pompe magasin douche
        sanitaire sanitaires voirie entree sortie usine masse indice dessine
        verifie approuve legende repere armatures ferraillage enrobage generale
        generales
    """.split()),
    "es": frozenset("""
        los las del con por para sobre bajo una unos unas en es este esta
        planta seccion corte nivel muro muros columna columnas viga vigas losa
        escalera escaleras puerta puertas ventana ventanas techo cubierta hormigon
        acero escala habitacion sala cocina edificio piso pisos agua tuberia
        drenaje ancho alto altura largo espesor norte sur oeste este proyecto
        cimentacion zapata
    """.split()),
    "de": frozenset("""
        der die das und mit von zu fur auf im ist sind nicht ein eine einer
        grundriss schnitt ansicht geschoss wand wande tur turen fenster dach treppe
        stahl beton balken stutze decke fundament massstab raum gebaude wasser
        rohr breite hohe lange dicke nord sud west ost
    """.split()),
    "it": frozenset("""
        il lo gli della delle dello degli con per sul sulla nel nella una sono
        pianta sezione prospetto livello muro muri pilastro pilastri trave travi
        solaio scala scale porta porte finestra finestre tetto calcestruzzo acciaio
        stanza cucina edificio acqua larghezza altezza lunghezza spessore progetto
        fondazione
    """.split()),
    "pt": frozenset("""
        da dos das em na nos nas com por para sobre um uma sao
        corte nivel parede paredes pilar pilares viga vigas laje escada escadas
        porta portas janela janelas telhado cobertura concreto betao aco escala
        quarto sala cozinha edificio piso agua largura altura comprimento
        espessura norte sul oeste leste projeto fundacao
    """.split()),
    "nl": frozenset("""
        de het een en van met op voor naar bij niet zijn is
        plattegrond doorsnede gevel verdieping muur deur deuren raam ramen dak trap
        staal beton balk kolom vloer fundering schaal kamer gebouw water breedte
        hoogte lengte dikte noord zuid oost
    """.split()),
}
# 同一个词出现在不止一种语言的词表里时，它对这些语言都不算证据。
_WORD_OWNERS: dict[str, frozenset[str]] = {}
for _lang, _words in _LEXICON.items():
    for _word in _words:
        _WORD_OWNERS[_word] = _WORD_OWNERS.get(_word, frozenset()) | {_lang}
_EXCLUSIVE_WORDS = {
    lang: frozenset(word for word in words if len(_WORD_OWNERS[word]) == 1)
    for lang, words in _LEXICON.items()
}

# 只有某几种语言才用的字母。英文没有重音字母，拿不到这类证据。
_LETTER_OWNERS: dict[str, frozenset[str]] = {
    "fr": frozenset("éèêëàâçùûüôîïœÿ"),
    "es": frozenset("áéíóúñü¿¡"),
    "pt": frozenset("áâãàçéêíóôõú"),
    "de": frozenset("äöüß"),
    "it": frozenset("àèéìíòóùú"),
}


def base_lang(code: str) -> str:
    """``"en-US"`` / ``" FR "`` → ``"en"`` / ``"fr"``."""
    return str(code or "").strip().lower().replace("_", "-").split("-", 1)[0]


def has_latin_words(text: str) -> bool:
    """True when ``text`` contains real Latin-script words, not just codes.

    A word counts when it stands alone (not glued to digits like ``M10``) and
    is not an all-caps abbreviation of up to 4 letters (``GB``, ``PVC``).
    """
    for match in _LATIN_WORD_RE.finditer(text):
        word = match.group(0)
        if len(word) >= 2 and not (word.isupper() and len(word) <= 4):
            return True
    return False


def _fold(word: str) -> str:
    decomposed = unicodedata.normalize("NFKD", word.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char)).replace("’", "'")


def latin_language_scores(text: str) -> dict[str, int]:
    """Evidence per Latin-script language: 2 per exclusive word, letters 1–2."""
    scores = dict.fromkeys(_LEXICON, 0)
    for raw in _WORD_RE.findall(text):
        word = _fold(raw)
        # 「l'eau」「d'entrée」：法文省音后的实词同样计数。
        if "'" in word:
            head, _, word = word.partition("'")
            if head in {"l", "d", "qu", "n", "s", "j", "c"}:
                scores["fr"] += 2
        for lang, words in _EXCLUSIVE_WORDS.items():
            if word in words:
                scores[lang] += 2
    seen_letters = {char for char in text.lower() if not char.isascii()}
    for char in seen_letters:
        owners = [lang for lang, letters in _LETTER_OWNERS.items() if char in letters]
        if not owners:
            continue
        weight = 2 if len(owners) == 1 else 1
        for lang in owners:
            scores[lang] += weight
    return scores


def classify_text_language(text: str, target_lang: str) -> str:
    """Return ``TARGET``, ``FOREIGN`` or ``UNSURE`` for one text and target."""
    code = base_lang(target_lang)
    if code in _CJK_TARGET_PATTERNS:
        if re.search(_CJK_TARGET_PATTERNS[code], text) and not has_latin_words(text):
            return TARGET
        return FOREIGN
    pattern = _SCRIPT_TARGET_PATTERNS.get(code)
    if pattern:
        return TARGET if re.search(pattern, text) and not has_latin_words(text) else FOREIGN
    if code not in LATIN_TARGET_LANGS:
        # 自定义或不认识的目标语言：没有依据，照常全部翻译（与以前一致）。
        return FOREIGN
    # 混排文字（如「Q235钢板」）里只要还有中日韩等非拉丁文字，就仍需翻译。
    if NON_LATIN_SCRIPT_RE.search(text):
        return FOREIGN
    if not _LATIN_LETTER_RE.search(text):
        return FOREIGN
    scores = latin_language_scores(text)
    target_score = scores.get(code, 0)
    other_best = max((score for lang, score in scores.items() if lang != code), default=0)
    # 先看词：大写短词（MUR、SOL、EAU）在下面会被当成缩写，但它们是实词。
    if other_best >= 2 and other_best > target_score:
        return FOREIGN
    if not has_latin_words(text):
        # 只有型号、规范号、缩写（M10、GB/T 700、PVC）：任何语言里都原样保留。
        return TARGET
    if target_score >= 2 and target_score > other_best:
        return TARGET
    return UNSURE


def target_script_pattern(target_lang: str) -> str | None:
    """目标语言自己的文字（中文的汉字、韩文的谚文、俄文的西里尔字母…）的正则；
    用拉丁字母书写或不认识的目标语言是 None。"""
    code = base_lang(target_lang)
    return _CJK_TARGET_PATTERNS.get(code) or _SCRIPT_TARGET_PATTERNS.get(code)


def is_residual_foreign_text(text: str, target_lang: str) -> bool:
    """Whether written-back text still looks untranslated for ``target_lang``.

    For CJK/other-script targets the leftover must be real Latin words, not
    part numbers or standard codes. For Latin targets the leftover is any
    remaining CJK/other non-Latin script.

    CAD 写回后扫残留外文、PDF 原位替换核对译文都用这一条。
    """
    if base_lang(target_lang) in LATIN_TARGET_LANGS:
        return bool(NON_LATIN_SCRIPT_RE.search(text))
    return has_latin_words(text) and classify_text_language(text, target_lang) != TARGET
