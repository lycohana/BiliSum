#!/usr/bin/env python3
"""Generate VidMind desktop icons from the canonical icon.svg design."""

from __future__ import annotations

from pathlib import Path
import platform
import shutil
import subprocess

from PIL import Image, ImageColor, ImageDraw


CANVAS_SIZE = 512
GRADIENT_START = ImageColor.getrgb("#6366F1")
GRADIENT_END = ImageColor.getrgb("#8B5CF6")
ACCENT = ImageColor.getrgb("#6366F1")
ACCENT_LIGHT = ImageColor.getrgb("#8B5CF6")
WHITE = ImageColor.getrgb("#FFFFFF")


def scale(value: float, size: int) -> int:
    return round(value / CANVAS_SIZE * size)


def lerp_color(start: tuple[int, int, int], end: tuple[int, int, int], t: float) -> tuple[int, int, int, int]:
    return (
        round(start[0] + (end[0] - start[0]) * t),
        round(start[1] + (end[1] - start[1]) * t),
        round(start[2] + (end[2] - start[2]) * t),
        255,
    )


def draw_linear_gradient(draw: ImageDraw.ImageDraw, size: int) -> None:
    inner_start = scale(56, size)
    inner_end = scale(456, size)
    usable_span = max(1, inner_end - inner_start)
    for y in range(inner_start, inner_end):
        t = (y - inner_start) / usable_span
        color = lerp_color(GRADIENT_START, GRADIENT_END, t)
        draw.line([(inner_start, y), (inner_end, y)], fill=color)


def draw_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    # Background rounded rect
    outer = (scale(56, size), scale(56, size), scale(456, size), scale(456, size))
    outer_radius = scale(100, size)

    # White inner card
    inner = (scale(120, size), scale(120, size), scale(392, size), scale(392, size))
    inner_radius = scale(56, size)

    # Play triangle
    play_points = [
        (scale(220, size), scale(170, size)),
        (scale(310, size), scale(256, size)),
        (scale(220, size), scale(342, size)),
    ]

    # Neural nodes (mind element)
    node1_center = (scale(340, size), scale(162, size))
    node1_radius = scale(6, size)
    node2_center = (scale(356, size), scale(146, size))
    node2_radius = scale(4, size)
    node3_center = (scale(360, size), scale(176, size))
    node3_radius = scale(3, size)

    # Draw background gradient
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(outer, radius=outer_radius, fill=255)

    gradient = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw_linear_gradient(ImageDraw.Draw(gradient), size)
    image.paste(gradient, (0, 0), mask)

    # White inner card
    draw.rounded_rectangle(inner, radius=inner_radius, fill=WHITE)

    # Play triangle
    draw.polygon(play_points, fill=(99, 102, 241, 230))  # #6366F1 with 0.9 opacity

    # Neural connection lines
    draw.line([node1_center, node2_center], fill=(139, 92, 246, 102), width=scale(2, size))
    draw.line([node1_center, node3_center], fill=(139, 92, 246, 77), width=scale(2, size))
    # This is a simpler approach: just draw the nodes without anti-aliasing

    # Neural nodes
    draw.ellipse(
        (node1_center[0] - node1_radius, node1_center[1] - node1_radius,
         node1_center[0] + node1_radius, node1_center[1] + node1_radius),
        fill=(139, 92, 246, 128),
    )
    draw.ellipse(
        (node2_center[0] - node2_radius, node2_center[1] - node2_radius,
         node2_center[0] + node2_radius, node2_center[1] + node2_radius),
        fill=(139, 92, 246, 77),
    )
    draw.ellipse(
        (node3_center[0] - node3_radius, node3_center[1] - node3_radius,
         node3_center[0] + node3_radius, node3_center[1] + node3_radius),
        fill=(139, 92, 246, 51),
    )

    return image


def generate_ico(output_path: Path) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    largest = draw_icon(256)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    largest.save(output_path, format="ICO", sizes=sizes)
    print(f"Generated ICO file: {output_path}")
    print("  Sizes: " + ", ".join(f"{w}x{h}" for w, h in sizes))


def generate_icns(output_path: Path) -> None:
    if platform.system() != "Darwin":
        print("Skipping ICNS generation: iconutil is only available on macOS.")
        return

    iconutil = shutil.which("iconutil")
    if not iconutil:
        print("Skipping ICNS generation: iconutil not found.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    iconset_dir = output_path.with_suffix(".iconset")
    if iconset_dir.exists():
        shutil.rmtree(iconset_dir)
    iconset_dir.mkdir(parents=True)

    icon_specs = [
        (16, "icon_16x16.png"),
        (32, "icon_16x16@2x.png"),
        (32, "icon_32x32.png"),
        (64, "icon_32x32@2x.png"),
        (128, "icon_128x128.png"),
        (256, "icon_128x128@2x.png"),
        (256, "icon_256x256.png"),
        (512, "icon_256x256@2x.png"),
        (512, "icon_512x512.png"),
        (1024, "icon_512x512@2x.png"),
    ]
    for icon_size, file_name in icon_specs:
        draw_icon(icon_size).save(iconset_dir / file_name, format="PNG")

    subprocess.run([iconutil, "-c", "icns", str(iconset_dir), "-o", str(output_path)], check=True)
    shutil.rmtree(iconset_dir)
    print(f"Generated ICNS file: {output_path}")


def generate_pngs(output_dir: Path) -> None:
    png_sizes = [16, 24, 32, 48, 64, 128, 256]
    output_dir.mkdir(parents=True, exist_ok=True)
    for px in png_sizes:
        draw_icon(px).save(output_dir / f"icon.png_{px}", format="PNG")
        print(f"Generated PNG {px}x{px}: {output_dir / f'icon.png_{px}'}")


def main() -> None:
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    output_dir = repo_root / "apps" / "desktop" / "build"
    output_dir.mkdir(parents=True, exist_ok=True)

    generate_ico(output_dir / "icon.ico")
    generate_pngs(output_dir)
    generate_icns(output_dir / "icon.icns")
    print("Done: all icon files generated.")


if __name__ == "__main__":
    main()
