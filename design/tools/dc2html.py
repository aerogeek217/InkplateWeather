"""Convert Design-canvas artboards (.dc.html) into plain, standalone HTML.

The canvas format wraps markup in <x-dc>/<helmet> and loads a runtime
(support.js) that only exists inside the Design artifact. The approved
boards are fully static, so we can unwrap them into ordinary HTML that any
browser (or a headless renderer on the Pi) can load.

Usage: python design/tools/dc2html.py <in.dc.html> <out.html> [<in> <out> ...]
"""
import re
import sys


def convert(src: str, title_fallback: str) -> str:
    title = re.search(r"<title>(.*?)</title>", src, re.S)
    title = title.group(1).strip() if title else title_fallback
    helmet = re.search(r"<helmet>(.*?)</helmet>", src, re.S)
    head_extra = helmet.group(1).strip() if helmet else ""
    body = re.search(r"</helmet>(.*?)</x-dc>", src, re.S)
    if not body:
        raise ValueError("no <x-dc> body found")
    if "{{" in body.group(1):
        raise ValueError("board still contains template holes; it is not static")
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{title}</title>\n"
        f"{head_extra}\n"
        "</head>\n<body>\n"
        f"{body.group(1).strip()}\n"
        "</body>\n</html>\n"
    )


def main(argv):
    if len(argv) < 2 or len(argv) % 2:
        sys.exit(__doc__)
    for src_path, out_path in zip(argv[::2], argv[1::2]):
        with open(src_path, encoding="utf-8") as f:
            html = convert(f.read(), out_path)
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(html)
        print(f"{src_path} -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1:])
