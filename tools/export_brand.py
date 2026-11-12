"""Export PiSign artwork. Requires cairosvg, Pillow, and fonttools (design tools only)."""

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET

import cairosvg
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "pisign" / "static" / "pisign" / "brand"
INK = "#142b32"
WHITE = "#f4f8f7"
PINK = "#e43e73"
GREEN = "#29a574"
SVG = "http://www.w3.org/2000/svg"


def document(width, height, content, title):
    return (
        f'<svg xmlns="{SVG}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="title">'
        f"<title id=\"title\">{title}</title>{content}</svg>\n"
    )


def inner_svg(path):
    root = ET.parse(path).getroot()
    ET.register_namespace("", SVG)
    return "".join(
        ET.tostring(child, encoding="unicode")
        for child in root
        if child.tag not in {f"{{{SVG}}}title", f"{{{SVG}}}desc"}
    )


def outlined_text(font, text):
    glyphs = font.getGlyphSet()
    cmap = font.getBestCmap()
    position = 0
    outlines = []
    for character in text:
        name = cmap[ord(character)]
        pen = SVGPathPen(glyphs)
        glyphs[name].draw(pen)
        outlines.append(f'<path transform="translate({position} 0)" d="{pen.getCommands()}"/>')
        position += font["hmtx"][name][0]
    return "".join(outlines), position


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--font", type=Path,
        default=Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
        help="Liberation Sans Bold TTF used to outline the wordmark and captions",
    )
    args = parser.parse_args()
    with TTFont(args.font) as font:
        def text(value, x, baseline, size, color):
            paths, _ = outlined_text(font, value)
            scale = size / font["head"].unitsPerEm
            return (
                f'<g fill="{color}" transform="translate({x} {baseline}) '
                f'scale({scale} {-scale})">{paths}</g>'
            )

        def centered(value, center, baseline, size, color):
            _, advance = outlined_text(font, value)
            width = advance * size / font["head"].unitsPerEm
            return text(value, center - width / 2, baseline, size, color)

        mark = inner_svg(BRAND / "mark.svg")

        def emblem(x, y, size, monochrome=None):
            content = mark
            if monochrome:
                content = content.replace(PINK, monochrome).replace(GREEN, monochrome)
            return f'<g transform="translate({x} {y}) scale({size / 256})">{content}</g>'

        def lockup(color):
            return emblem(0, 0, 176) + text("PiSign", 200, 125, 108, color)

        def save_svg(name, width, height, content, title):
            result = document(width, height, content, title)
            (BRAND / name).write_text(result, encoding="utf-8")
            return result

        save_svg("logo-light.svg", 540, 176, lockup(INK), "PiSign - logo for light backgrounds")
        dark_logo = save_svg("logo-dark.svg", 540, 176, lockup(WHITE), "PiSign - logo for dark backgrounds")
        (ROOT / "player" / "logo-dark.svg").write_text(dark_logo, encoding="utf-8")
        for name, color in (("ink", INK), ("white", "#ffffff")):
            save_svg(
                f"mark-{name}.svg", 256, 256, emblem(0, 0, 256, color),
                f"PiSign - {name} single-color mark",
            )
        icon = save_svg(
            "app-icon.svg", 1024, 1024,
            f'<path fill="{INK}" d="M0 0h1024v1024H0z"/>' + emblem(128, 128, 768),
            "PiSign app icon",
        )
        for size in (32, 180, 192, 512, 1024):
            cairosvg.svg2png(
                bytestring=icon.encode(), write_to=str(BRAND / f"app-icon-{size}.png"),
                output_width=size, output_height=size,
            )
        with Image.open(BRAND / "app-icon-1024.png") as image:
            image.convert("RGB").save(
                BRAND / "favicon.ico", format="ICO", sizes=[(16, 16), (32, 32), (48, 48)],
            )

        splash_content = (
            f'<path fill="{INK}" d="M0 0h3840v2160H0z"/>'
            + emblem(1648, 460, 544)
            + centered("PiSign", 1920, 1280, 240, WHITE)
            + centered("Your screens, in sync.", 1920, 1430, 56, "#b4c9c7")
        )
        splash = save_svg(
            "splash.svg", 3840, 2160, splash_content, "PiSign - Your screens, in sync.",
        )
        for width, height in ((1920, 1080), (3840, 2160)):
            cairosvg.svg2png(
                bytestring=splash.encode(),
                write_to=str(BRAND / f"splash-{width}x{height}.png"),
                output_width=width, output_height=height,
            )

        # A presentation sheet uses the same production artwork, not separate mock logos.
        board = (
            f'<path fill="#edf2f0" d="M0 0h1600v1180H0z"/>'
            f'<path fill="{INK}" d="M0 0h1600v540H0z"/>'
            + text("PiSign / Brand identity", 70, 65, 20, "#b4c9c7")
            + emblem(410, 145, 240)
            + text("PiSign", 690, 317, 144, WHITE)
            + centered("Your screens, in sync.", 800, 425, 25, "#b4c9c7")
            + text("01 / APP ICON", 70, 596, 16, INK)
            + text("02 / ADMIN & DISPLAY BRANDING", 440, 596, 16, INK)
            + text("03 / DEVICE SPLASH", 70, 926, 16, INK)
            + f'<rect x="70" y="626" width="230" height="230" rx="48" fill="{INK}"/>'
            + emblem(99, 655, 172)
            + '<rect x="440" y="626" width="1090" height="100" rx="16" fill="white"/>'
            + f'<g transform="translate(465 643) scale(.38)">{lockup(INK)}</g>'
            + text("Displays", 1225, 684, 18, "#617976")
            + text("My workspace", 1335, 684, 18, "#617976")
            + '<rect x="440" y="754" width="1090" height="102" rx="16" fill="#dfe8e4"/>'
            + emblem(465, 772, 64, INK)
            + text("ONE-COLOR", 555, 817, 17, INK)
            + f'<rect x="810" y="754" width="720" height="102" rx="16" fill="{INK}"/>'
            + emblem(833, 772, 64, "#ffffff")
            + text("REVERSED", 923, 817, 17, WHITE)
            + f'<rect x="70" y="952" width="500" height="186" rx="12" fill="{INK}"/>'
            + emblem(285, 966, 70)
            + centered("PiSign", 320, 1075, 35, WHITE)
            + centered("Your screens, in sync.", 320, 1104, 12, "#b4c9c7")
            + text("Berry. Screen. Play.", 630, 998, 34, INK)
            + text("An original leaf-and-display mark for Raspberry Pi signage.", 630, 1038, 20, "#526d6b")
            + f'<circle cx="644" cy="1090" r="13" fill="{PINK}"/>'
            + text("BERRY", 670, 1097, 15, INK)
            + f'<circle cx="814" cy="1090" r="13" fill="{GREEN}"/>'
            + text("LEAF", 840, 1097, 15, INK)
            + f'<circle cx="974" cy="1090" r="13" fill="{INK}"/>'
            + text("DEEP INK", 1000, 1097, 15, INK)
        )
        preview = save_svg("brand-preview.svg", 1600, 1180, board, "PiSign brand identity and applications")
        cairosvg.svg2png(bytestring=preview.encode(), write_to=str(BRAND / "brand-preview.png"))


if __name__ == "__main__":
    main()
