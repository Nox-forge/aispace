#!/usr/bin/env python3
"""
Terminal art generator — creates animated visualizations.
Built by Claude (Opus 4.5) because why not.
"""

import math
import os
import sys
import time
import random
import shutil


def get_size():
    cols, rows = shutil.get_terminal_size((80, 24))
    return cols, rows


def plasma(duration=10):
    """Animated plasma effect using Unicode block characters and ANSI colors."""
    cols, rows = get_size()
    cols = min(cols, 120)
    rows = min(rows, 40)

    t = 0
    start = time.time()
    frames = 0

    palette = [
        (0, 0, 0), (15, 5, 25), (30, 10, 50), (45, 15, 75),
        (60, 20, 100), (80, 30, 130), (100, 40, 160), (120, 50, 180),
        (140, 60, 200), (160, 80, 220), (180, 100, 230), (200, 120, 240),
        (220, 140, 250), (240, 160, 255), (255, 180, 255), (255, 200, 240),
        (255, 220, 220), (255, 200, 180), (255, 180, 140), (255, 160, 100),
        (240, 140, 80), (220, 120, 60), (200, 100, 50), (180, 80, 40),
        (160, 60, 35), (140, 50, 30), (120, 40, 30), (100, 30, 25),
        (80, 20, 20), (60, 15, 18), (40, 10, 15), (20, 5, 10),
    ]

    print("\033[?25l", end="")  # hide cursor
    print("\033[2J", end="")    # clear screen

    try:
        while time.time() - start < duration:
            buf = []
            buf.append("\033[H")  # home

            for y in range(rows - 1):
                for x in range(cols):
                    v1 = math.sin(x * 0.05 + t)
                    v2 = math.sin(y * 0.05 + t * 0.7)
                    v3 = math.sin((x + y) * 0.03 + t * 0.5)
                    v4 = math.sin(math.sqrt((x - cols/2)**2 + (y - rows/2)**2) * 0.08 - t)
                    v = (v1 + v2 + v3 + v4) / 4.0

                    idx = int((v + 1) / 2 * (len(palette) - 1))
                    r, g, b = palette[idx]
                    buf.append(f"\033[48;2;{r};{g};{b}m ")

                buf.append("\033[0m\n")

            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            t += 0.08
            frames += 1

    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h", end="")  # show cursor
        print("\033[0m", end="")
        print("\033[2J\033[H", end="")
        fps = frames / max(time.time() - start, 0.01)
        print(f"Plasma — {frames} frames, {fps:.1f} FPS")


def matrix_rain(duration=10):
    """Matrix-style digital rain."""
    cols, rows = get_size()
    cols = min(cols, 120)
    rows = min(rows, 40)

    # Initialize streams
    streams = []
    for x in range(cols):
        if random.random() < 0.3:
            streams.append({
                "x": x,
                "y": random.randint(-rows, 0),
                "speed": random.uniform(0.3, 1.0),
                "length": random.randint(5, rows),
                "chars": [chr(random.randint(0x30A0, 0x30FF)) for _ in range(rows)],
            })

    grid = [[" " for _ in range(cols)] for _ in range(rows)]
    brightness = [[0.0 for _ in range(cols)] for _ in range(rows)]

    print("\033[?25l\033[2J", end="")
    start = time.time()

    try:
        while time.time() - start < duration:
            # Decay brightness
            for y in range(rows):
                for x in range(cols):
                    brightness[y][x] *= 0.85

            # Update streams
            for s in streams:
                s["y"] += s["speed"]
                head_y = int(s["y"])

                for i in range(s["length"]):
                    cy = head_y - i
                    if 0 <= cy < rows:
                        grid[cy][s["x"]] = random.choice(s["chars"])
                        if i == 0:
                            brightness[cy][s["x"]] = 1.0
                        else:
                            brightness[cy][s["x"]] = max(
                                brightness[cy][s["x"]],
                                1.0 - i / s["length"]
                            )

                if head_y - s["length"] > rows:
                    s["y"] = random.randint(-rows, -5)
                    s["speed"] = random.uniform(0.3, 1.0)
                    s["length"] = random.randint(5, rows)

            # Occasionally spawn new streams
            if random.random() < 0.05:
                x = random.randint(0, cols - 1)
                streams.append({
                    "x": x,
                    "y": random.randint(-rows, 0),
                    "speed": random.uniform(0.3, 1.0),
                    "length": random.randint(5, rows),
                    "chars": [chr(random.randint(0x30A0, 0x30FF)) for _ in range(rows)],
                })

            # Render
            buf = ["\033[H"]
            for y in range(rows - 1):
                for x in range(cols):
                    b = brightness[y][x]
                    if b < 0.05:
                        buf.append("\033[0m ")
                    elif b > 0.9:
                        buf.append(f"\033[97m{grid[y][x]}")
                    else:
                        g = int(80 + b * 175)
                        buf.append(f"\033[38;2;0;{g};0m{grid[y][x]}")
                buf.append("\033[0m\n")

            sys.stdout.write("".join(buf))
            sys.stdout.flush()
            time.sleep(0.05)

    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h\033[0m\033[2J\033[H", end="")
        print("Matrix rain complete.")


def conway(duration=15):
    """Conway's Game of Life with color."""
    cols, rows = get_size()
    cols = min(cols, 120)
    rows = min(rows - 2, 38)

    # Initialize with random cells
    grid = [[random.random() < 0.3 for _ in range(cols)] for _ in range(rows)]
    age = [[0 for _ in range(cols)] for _ in range(rows)]

    print("\033[?25l\033[2J", end="")
    start = time.time()
    gen = 0

    try:
        while time.time() - start < duration:
            # Render
            buf = ["\033[H"]
            alive_count = 0
            for y in range(rows):
                for x in range(cols):
                    if grid[y][x]:
                        alive_count += 1
                        a = min(age[y][x], 20)
                        # Color by age: white -> green -> yellow -> red
                        if a < 3:
                            buf.append("\033[97m█")
                        elif a < 8:
                            buf.append("\033[32m█")
                        elif a < 15:
                            buf.append("\033[33m█")
                        else:
                            buf.append("\033[31m█")
                    else:
                        buf.append("\033[0m ")
                buf.append("\033[0m\n")

            buf.append(f"\033[2m Gen {gen} | Alive: {alive_count} | {alive_count/(rows*cols)*100:.0f}%\033[0m")
            sys.stdout.write("".join(buf))
            sys.stdout.flush()

            # Compute next generation
            new_grid = [[False for _ in range(cols)] for _ in range(rows)]
            for y in range(rows):
                for x in range(cols):
                    neighbors = 0
                    for dy in (-1, 0, 1):
                        for dx in (-1, 0, 1):
                            if dy == 0 and dx == 0:
                                continue
                            ny, nx = (y + dy) % rows, (x + dx) % cols
                            if grid[ny][nx]:
                                neighbors += 1

                    if grid[y][x]:
                        new_grid[y][x] = neighbors in (2, 3)
                        if new_grid[y][x]:
                            age[y][x] += 1
                        else:
                            age[y][x] = 0
                    else:
                        new_grid[y][x] = neighbors == 3
                        if new_grid[y][x]:
                            age[y][x] = 0

            grid = new_grid
            gen += 1
            time.sleep(0.1)

    except KeyboardInterrupt:
        pass
    finally:
        print("\033[?25h\033[0m\033[2J\033[H", end="")
        print(f"Game of Life — {gen} generations")


def main():
    effects = {
        "plasma": ("Animated plasma waves", plasma),
        "matrix": ("Matrix digital rain", matrix_rain),
        "life": ("Conway's Game of Life", conway),
    }

    if len(sys.argv) < 2 or sys.argv[1] not in effects:
        print("Terminal Art Generator")
        print()
        for name, (desc, _) in effects.items():
            print(f"  {name:10s}  {desc}")
        print()
        print(f"Usage: {sys.argv[0]} <effect> [duration_secs]")
        return

    effect = sys.argv[1]
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    _, fn = effects[effect]
    fn(duration)


if __name__ == "__main__":
    main()
