"""Оформление обложки дайджеста: сезон месяца и дизайн дня.

Чтобы обложки не были одинаковыми, оформление выбирает код по дате выпуска:
у каждого месяца своя сезонная тема (ранняя осень, осень, поздняя осень…),
а внутри месяца по очереди идёт 31 дизайн — стиль рисунка, вёрстка
заголовков и ракурс. Дизайн дня совмещается с темой месяца. Агент пишет в
заготовке только сцену и заголовки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

HERO = (
    "Main character: the classic Go gopher mascot in Renée French's style — a chubby"
    " light-blue body, big round white eyes with small black pupils, a small beige snout"
    " with two white buck teeth, tiny ears, short arms and feet. Keep it recognizable as"
    " the official Go gopher in any art style."
)
MOOD = (
    "Mood: a breaking-news cover — a frozen moment in the middle of the action, motion"
    " lines, flying sparks and debris, strong contrast lighting. The gopher is mid-motion"
    " with a clear emotion — shocked, determined or racing against time. Energetic, not"
    " calm."
)
TEXT_RULES = (
    "All text is large, high-contrast and readable on a phone screen; the art style"
    " must never make the letters harder to read."
)
SCENE_HINT = (
    "<по-английски 2–3 предложения: стоп-кадр в разгар действия — что сейчас происходит,"
    " что гофер делает прямо в эту секунду и чем это грозит; предметы — метафоры главных"
    " новостей дня; место действия и погода — из строки Season>"
)
PLACEHOLDER = re.compile(r"<[^<>\n]{3,}>")

# Сезон месяца: (название, где и когда происходит сцена, палитра).
SEASONS = (
    (
        "глубокая зима",
        "deep winter, January — a frosty clear day, deep snowdrifts, icicles, frost"
        " patterns, the gopher's breath turns to steam",
        "icy blue, white and deep navy with one warm orange accent",
    ),
    (
        "поздняя зима",
        "late winter, February — a blizzard at dusk, wind-blown snow, glowing windows,"
        " the gopher in a knitted scarf",
        "violet-blue twilight, snow white and warm lamp yellow",
    ),
    (
        "ранняя весна",
        "early spring, March — melting snow, dripping icicles, puddles and running"
        " streams, the first bare ground under a bright sun",
        "pale sky blue, wet grey and fresh yellow-green",
    ),
    (
        "весна",
        "mid spring, April — a sudden spring shower, first green leaves and buds,"
        " a rainbow breaking through the clouds",
        "fresh green, rain blue and pink blossom",
    ),
    (
        "поздняя весна",
        "late spring, May — everything in bloom, lilac and apple blossom, petals flying"
        " in a warm wind",
        "lilac, blossom white and juicy grass green",
    ),
    (
        "раннее лето",
        "early summer, June — a long bright evening, tall meadow grass, dandelion fluff"
        " and fireflies in the air",
        "sunny yellow, meadow green and soft sky blue",
    ),
    (
        "лето",
        "high summer, July — blazing midday heat, shimmering hot air, a thunderstorm"
        " building on the horizon",
        "hot orange, turquoise and deep storm blue",
    ),
    (
        "позднее лето",
        "late summer, August — a golden harvest evening, ripe wheat and sunflowers,"
        " shooting stars in the early dusk",
        "wheat gold, sunflower yellow and warm dusk purple",
    ),
    (
        "ранняя осень",
        "early autumn, September — crisp clear air, the first yellow leaves, spider"
        " silk in the low sun, rowan berries",
        "golden yellow, rowan red and clear sky blue",
    ),
    (
        "осень",
        "mid autumn, October — peak leaf fall, a storm of red and orange leaves in the"
        " wind, rain puddles and pumpkins",
        "burnt orange, crimson and dark teal",
    ),
    (
        "поздняя осень",
        "late autumn, November — bare trees, fog and cold rain, the first wet"
        " snowflakes, street lamps lit early",
        "foggy grey, dark plum and amber lamp light",
    ),
    (
        "ранняя зима",
        "early winter, December — the first real snowfall, fairy lights and garlands,"
        " frosty breath, a festive evening",
        "pine green, snow white and festive red with warm gold lights",
    ),
)

# Стиль рисунка: по одному на день месяца.
STYLES = (
    ("flat-vector", "flat vector illustration, bold clean shapes, vivid saturated colors"),
    ("paper-cut", "layered paper cut-out collage, visible paper texture, soft drop shadows"),
    ("risograph", "two-colour risograph print, grainy ink texture, slight misregistration"),
    ("comic", "comic-book panel, bold ink outlines, halftone dots, action onomatopoeia shapes"),
    ("isometric", "isometric low-poly 3D diorama on a floating tile, soft studio light"),
    ("pixel-art", "16-bit pixel art like a classic console game, crisp pixels, limited palette"),
    ("linocut", "hand-carved linocut print, rough carved lines, two or three flat inks"),
    ("claymation", "plasticine stop-motion scene, fingerprints in the clay, miniature set"),
    ("gouache", "gouache children's-book illustration, visible brush strokes, matte colors"),
    ("travel-poster", "retro 1960s travel poster, simplified shapes, limited screen-print inks"),
    ("synthwave", "neon synthwave, glowing outlines, grid horizon, deep dark background"),
    ("blueprint", "technical blueprint drawing, white line art on a tinted sheet, one accent"),
    ("sticker", "die-cut sticker art, thick white outlines, glossy highlights, bright fills"),
    ("constructivist", "constructivist poster, sharp diagonals, bold geometry, photomontage feel"),
    ("woodblock", "Japanese ukiyo-e woodblock print, flowing outlines, flat graded colors"),
    ("chalkboard", "coloured chalk drawing on a dark blackboard, dusty smudges, hand lettering"),
    ("stained-glass", "stained-glass window, thick dark lead lines, glowing translucent colors"),
    ("felt", "handmade felt and wool craft, stitched edges, soft fuzzy texture, button details"),
    ("pop-art", "pop art, Ben-Day dots, thick black outlines, loud clashing colors"),
    ("art-deco", "art deco poster, elegant symmetry, sunburst rays, metallic gold accents"),
    ("doodle", "marker doodle on squared notebook paper, wobbly lines, highlighter fills"),
    ("origami", "folded origami paper figures, sharp creases, low-poly facets, soft shadows"),
    ("cel-cartoon", "Saturday-morning cel animation frame, clean outlines, flat cel shading"),
    ("mid-century", "mid-century modern illustration, textured geometric shapes, off-register ink"),
    ("manga", "black-and-white manga action page, speed lines, screentone, one spot colour"),
    ("voxel", "voxel 3D scene built from tiny cubes, tilt-shift depth of field"),
    ("graffiti", "graffiti mural on a brick wall, spray-paint drips, bold wildstyle shapes"),
    ("pulp", "vintage pulp sci-fi magazine cover, dramatic painted light, aged paper"),
    ("embroidery", "embroidered patch, visible satin stitches, thread texture, stitched border"),
    ("vinyl-toy", "glossy 3D render of collectible vinyl toys, smooth plastic, studio rim light"),
    ("board-game", "painted tabletop board-game box art, rich detail, heroic composition"),
)

# Вёрстка текста и ракурс: идут своим шагом, поэтому сочетания не повторяются.
LAYOUTS = (
    "the title at the top in a bold rounded display font, the date in a small pill badge"
    " under it; the headlines stacked as rounded sticker cards on the right of the gopher",
    "the title on a wide ribbon banner across the top, the date on a round stamp; the"
    " headlines on tilted paper notes pinned down the left side",
    "the title in a bottom banner with the date as a tag beside it; the headlines in"
    " speech bubbles flying around the gopher in the upper half",
    "the title set vertically along the left edge, the date in a corner flag; the"
    " headlines as signpost boards on a pole on the right",
    "the title in a top-left block like a magazine masthead, the date under it in small"
    " caps; the headlines as cover lines in a column down the left, the gopher on the right",
    "the title arched over the scene, the date on a small ticket; the headlines on a"
    " strip of cards along the bottom edge like a film strip",
    "the title on a torn-paper strip at the top, the date as a circled note; the"
    " headlines on cards scattered diagonally from top right to bottom left",
)
CAMERAS = (
    "dramatic low angle looking up at the gopher",
    "tilted Dutch angle, the horizon at a steep slant",
    "top-down bird's-eye view of the action",
    "extreme close-up with the gopher bursting toward the viewer",
    "wide shot with the gopher small against a huge looming threat",
)


@dataclass(frozen=True)
class Design:
    """Оформление обложки одного выпуска."""

    season: str  # «ранняя осень»
    style: str  # короткое имя стиля: «paper-cut»
    lines: tuple[str, ...]  # строки промпта, которые агент не меняет

    @property
    def name(self) -> str:
        return f"{self.season} · {self.style}"


def design_for(day: date) -> Design:
    """Дизайн выпуска: сезон по месяцу, стиль, вёрстка и ракурс — по дню.

    Сдвиг на месяц: один и тот же день в соседних месяцах выглядит по-разному,
    а внутри месяца стиль не повторяется.
    """
    season, setting, palette = SEASONS[day.month - 1]
    n = day.day - 1
    style, look = STYLES[(n + 5 * day.month) % len(STYLES)]
    layout = LAYOUTS[(n + day.month) % len(LAYOUTS)]
    camera = CAMERAS[(n + 2 * day.month) % len(CAMERAS)]
    return Design(
        season=season,
        style=style,
        lines=(
            f"Season: {setting}. The season sets the place, the weather and the light.",
            f"Art style: {look}. Colour palette: {palette}.",
            f"Layout: {layout}. Each headline has a small simple icon that matches its"
            " topic. The gopher takes about half of the poster.",
            f"Camera: {camera}.",
        ),
    )


def render_prompt(day: date, date_text: str) -> str:
    """Заготовка image_prompt.md: оформление дня готово, сцену и заголовки пишет агент."""
    season, style, layout, camera = design_for(day).lines
    return "\n".join(
        [
            "Create a square 1:1 poster for the cover of a developer news digest.",
            "",
            HERO,
            "",
            season,
            "",
            f"Scene: {SCENE_HINT}",
            "",
            "Text on the image (in Russian, render every word exactly as written, with"
            " correct Cyrillic spelling, nothing added):",
            'Title: "Дайджест разработчика"',
            f'Date: "{date_text}"',
            "Headlines:",
            '- "<заголовок 1>"',
            '- "<заголовок 2>"',
            '- "<заголовок 3>"',
            "",
            style,
            "",
            f"{layout} {TEXT_RULES}",
            "",
            camera,
            "",
            MOOD,
            "",
            "Do not add any other text, links, URLs, logos, emoji or code. No people, no"
            " screens with code.",
            "",
        ]
    )


def check_design(prompt: str, day: date) -> list[str]:
    """Ошибки оформления: заготовка не заполнена или дизайн дня подменён."""
    left = PLACEHOLDER.findall(prompt)
    if left:
        return [f"не заполнено {left[0][:40]}… — напиши сцену и заголовки"]
    design = design_for(day)
    flat = " ".join(prompt.split())
    return [
        f"нет строки оформления дня ({design.name}): «{line[:60]}…» — верни её из"
        f" заготовки: ./narrate news cover --date {day.isoformat()}"
        for line in design.lines
        if line not in flat
    ]
