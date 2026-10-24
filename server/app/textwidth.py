"""Text width of the bundled Inter Tight, so generated text can be fitted before the browser lays it out.

The advance widths below were generated once from `assets/fonts/InterTight-latin.woff2` (a variable font) with
Pillow + FreeType (`python -m app.textwidth` regenerates them). A table keeps the view model independent of the
host's FreeType build (woff2 needs brotli) and makes fitting identical on Windows and in the container.
Kerning is ignored: Chromium's kerning only ever narrows these strings slightly, so measurements err wide.
"""
from __future__ import annotations

CHARS = (
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefghijklmnopqrstuvwxyz{|}~"
    "°–—−·’‘“”…×↑↓"
)
UNITS = 1000  # advances are in 1/1000 em
# weight → advances for CHARS, in order
_TABLE: dict[int, str] = {
    # BEGIN GENERATED
    300: "250,263,406,601,597,768,591,258,314,314,469,621,257,421,249,314,582,360,579,599,594,558,566,525,576,567,249,259,621,620,621,466,906,624,616,694,686,562,556,710,707,228,504,609,532,852,730,725,595,725,601,598,602,714,625,902,591,616,587,314,314,314,414,413,384,525,568,527,568,547,319,569,553,198,200,494,199,827,549,560,568,568,326,482,326,546,514,774,496,514,499,381,277,382,621,423,500,1000,621,249,249,249,415,415,798,621,923,923",  # noqa: E501
    400: "234,250,415,584,591,766,592,249,313,313,453,613,251,412,238,310,578,355,559,590,595,556,568,515,566,568,238,251,612,613,612,460,889,629,603,681,671,550,540,696,692,216,495,605,515,845,706,715,587,715,592,591,594,693,629,909,595,619,578,314,310,314,421,406,388,518,561,514,561,532,314,562,546,190,190,497,191,824,540,549,561,561,325,475,316,535,510,766,493,510,494,374,280,375,612,407,500,1000,612,238,249,249,429,429,775,612,907,907",  # noqa: E501
    600: "211,292,489,606,610,806,625,289,334,334,506,636,297,427,282,341,630,384,584,614,629,594,610,539,611,610,282,293,635,636,636,507,962,685,620,707,687,570,549,718,707,238,523,640,528,871,704,737,606,738,613,610,622,696,686,972,652,668,615,334,341,335,443,432,417,540,585,541,586,553,341,587,576,223,224,530,222,861,573,570,586,586,359,510,343,570,539,800,525,539,525,417,321,417,636,420,500,1000,635,282,298,298,514,508,769,635,917,917",  # noqa: E501
    700: "199,313,527,617,621,826,641,309,343,343,532,647,320,436,304,357,656,399,597,627,645,614,631,551,632,631,304,313,647,647,647,529,998,714,628,720,695,580,552,728,713,247,537,657,535,883,702,749,615,750,624,621,636,697,714,1003,682,693,634,344,357,344,454,445,432,551,597,554,598,563,355,599,593,240,241,549,239,880,590,581,598,598,375,529,355,589,553,817,541,553,539,439,342,439,647,427,500,1000,647,304,322,322,557,548,766,647,921,921",  # noqa: E501
    800: "188,335,564,628,632,846,657,329,353,354,559,658,343,444,327,373,681,414,610,639,662,632,650,564,653,650,327,334,658,658,658,554,1034,743,636,732,701,589,557,738,720,257,550,674,541,896,701,760,624,762,634,632,650,697,743,1035,710,716,653,354,373,354,465,457,447,562,610,568,611,574,369,613,610,256,256,566,256,900,608,592,610,610,393,547,369,607,567,834,557,567,554,461,361,461,658,434,500,1000,658,327,347,347,600,588,764,658,926,926",  # noqa: E501
    # END GENERATED
}
WIDTHS: dict[int, dict[str, int]] = {
    w: dict(zip(CHARS, (int(x) for x in s.split(",")), strict=True)) for w, s in _TABLE.items()
}
_FALLBACK = "M"  # unknown characters are measured as a wide glyph


def _weight(weight: int) -> dict[str, int]:
    if weight in WIDTHS:
        return WIDTHS[weight]
    return WIDTHS[min(WIDTHS, key=lambda w: abs(w - weight))]


def measure(text: str, size: float, weight: int = 400, letter_spacing_em: float = 0.0,
            uppercase: bool = False) -> float:
    """Width in CSS px of `text` set in Inter Tight at `size` px."""
    table = _weight(weight)
    s = text.upper() if uppercase else text
    units = sum(table.get(c, table[_FALLBACK]) for c in s)
    return units * size / UNITS + len(s) * letter_spacing_em * size


def _generate() -> str:  # pragma: no cover - maintenance tool
    from PIL import ImageFont

    from .paths import ASSETS_DIR

    lines = []
    for w in (300, 400, 600, 700, 800):
        f = ImageFont.truetype(str(ASSETS_DIR / "fonts" / "InterTight-latin.woff2"), UNITS)
        f.set_variation_by_axes([w])
        lines.append(f'    {w}: "' + ",".join(str(round(f.getlength(c))) for c in CHARS) + '",  # noqa: E501')
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    import re
    from pathlib import Path

    p = Path(__file__)
    src = p.read_text(encoding="utf-8")
    body = _generate()
    src = re.sub(r"(# BEGIN GENERATED\n).*?( *# END GENERATED)", lambda m: m.group(1) + body + "\n" + m.group(2),
                 src, flags=re.S)
    p.write_text(src, encoding="utf-8", newline="\n")
    print("updated", p)
