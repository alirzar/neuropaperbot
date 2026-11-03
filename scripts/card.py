"""Draw the card image for a draft: the bot's name, the title, who and where, and the start of the abstract.

The card is the thumbnail of the Bluesky link card and the photo in the Telegram
review message. Preview cards without posting anything:
  python scripts/card.py                    cards for the latest drafts, saved in previews/
  python scripts/card.py --title "A title"  one card with a made-up paper, to try layouts
"""
import argparse
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from common import DRAFTS, ROOT, load_config, read_json

FONTS = ROOT / "assets" / "fonts"
SERIF = str(FONTS / "Lora-Regular.ttf")
SANS = str(FONTS / "Inter-Regular.ttf")
PREVIEWS = ROOT / "previews"

# Palette: tweak here.
BG = "#141a33"  # deep indigo
INK = "#f3f0e8"  # warm white
BODY = "#d6d3cc"  # abstract text, a little dimmer than INK
MUTED = "#a9b1cf"  # soft slate
ACCENT = "#5ad1c2"  # teal: the name, and journal names
PREPRINT = "#f2b84b"  # amber: preprints stand out as not yet peer reviewed

W, H = 1200, 675
MARGIN = 80
MAXW = W - 2 * MARGIN
TITLE_SIZES = (58, 52, 46, 42, 38)  # largest that fits the title in four lines wins
FOOTER_Y = H - 50


def font(path, size):
    return ImageFont.truetype(path, size)


def wrap(draw, text, fnt, max_width):
    """Greedy word wrap by measured width; a word too long for a line is split."""
    lines, line = [], ""
    for word in text.split():
        while draw.textlength(word, font=fnt) > max_width:  # e.g. a long gene or compound name
            cut = len(word)
            while cut > 1 and draw.textlength(word[:cut], font=fnt) > max_width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        trial = f"{line} {word}" if line else word
        if draw.textlength(trial, font=fnt) <= max_width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def clip(draw, lines, count, fnt, max_width):
    """The first `count` lines, with an ellipsis on the last one if anything was dropped."""
    if len(lines) <= count:
        return lines
    kept = lines[:count]
    last = kept[-1]
    while last and draw.textlength(last + "…", font=fnt) > max_width:
        last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
    kept[-1] = last.rstrip(" ,;:.") + "…"
    return kept


def byline(authors):
    if not authors:
        return ""
    if len(authors) == 1:
        return authors[0]
    if len(authors) == 2:
        return f"{authors[0]} and {authors[1]}"
    return f"{authors[0]} et al."


def make_card(d, name):
    """PNG bytes for draft `d` (needs title, authors, venue, source, doi; abstract optional)."""
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle((0, 0, 11, H), fill=ACCENT)

    # The bot's name, letter-spaced, above a short rule.
    x, wordmark = MARGIN, font(SANS, 24)
    for ch in name.upper():
        draw.text((x, 92), ch, font=wordmark, fill=ACCENT, anchor="ls")
        x += draw.textlength(ch, font=wordmark) + 3
    draw.rectangle((MARGIN, 112, MARGIN + 90, 115), fill=ACCENT)

    for size in TITLE_SIZES:
        title_font = font(SERIF, size)
        lines = wrap(draw, d["title"], title_font, MAXW)
        if len(lines) <= 4:
            break
    lines = clip(draw, lines, 4, title_font, MAXW)
    line_height = round(size * 1.2)
    y = 140 + round(size * 0.7)
    for line in lines:
        draw.text((MARGIN, y), line, font=title_font, fill=INK, anchor="ls")
        y += line_height
    y += 56 - line_height

    # "Smith et al.  ·  Neuron", the venue in amber for preprints.
    meta = font(SANS, 28)
    x = MARGIN
    who = byline(d.get("authors"))
    if who:
        who += "  ·  "
        draw.text((x, y), who, font=meta, fill=MUTED, anchor="ls")
        x += draw.textlength(who, font=meta)
    venue_color = PREPRINT if d.get("source") == "biorxiv" else ACCENT
    draw.text((x, y), d.get("venue") or "", font=meta, fill=venue_color, anchor="ls")

    # As much of the abstract as fits above the footer, up to four lines.
    body = font(SERIF, 26)
    first = y + 58
    room = max(0, (FOOTER_Y - 52 - first) // 38 + 1)
    if d.get("abstract") or d.get("snippet"):
        text = d.get("abstract") or d.get("snippet")
        for i, line in enumerate(clip(draw, wrap(draw, text, body, MAXW), min(4, room), body, MAXW)):
            draw.text((MARGIN, first + i * 38), line, font=body, fill=BODY, anchor="ls")

    draw.text((MARGIN, FOOTER_Y), f"doi.org/{d['doi']}", font=font(SANS, 22), fill=MUTED, anchor="ls")

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def main():
    parser = argparse.ArgumentParser(description="Save card previews in previews/ (posts nothing).")
    parser.add_argument("--title", help="draw one card for a made-up paper with this title")
    parser.add_argument("--last", type=int, default=5, help="how many recent drafts (default 5)")
    args = parser.parse_args()
    name = load_config().get("name", "Neuro Paper Bot")

    if args.title:
        drafts = [
            {
                "id": "sample",
                "title": args.title,
                "authors": ["Smith", "Jones", "Lee"],
                "venue": "bioRxiv preprint",
                "source": "biorxiv",
                "doi": "10.64898/2026.11.02.123456",
                "abstract": "Here we show, using a made-up abstract, how the start of the abstract fills the space "
                "below the title. Longer titles leave less room, and the text stops with an ellipsis.",
            }
        ]
    else:
        drafts = read_json(DRAFTS, [])[-args.last :]
        if not drafts:
            raise SystemExit("No drafts yet; try --title to draw a sample card.")
    PREVIEWS.mkdir(exist_ok=True)
    for d in drafts:
        path = PREVIEWS / f"{d['id']}.png"
        path.write_bytes(make_card(d, name))
        print(f"{path}  ({path.stat().st_size // 1024} KB)  {d['title'][:70]}")


if __name__ == "__main__":
    main()
