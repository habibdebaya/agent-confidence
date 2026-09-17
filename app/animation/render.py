from __future__ import annotations

import argparse
import importlib.util
import json
import math
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.dont_write_bytecode = True

OUT = Path(__file__).resolve().parent
S = 2
BG = '#f7f6f2'
INK = '#193849'
MUTED = '#7c888e'
TEAL = '#19746a'
ORANGE = '#bf5c35'
PALE_ORANGE = '#fcf0e7'
PALE_BLUE = '#e9eff2'


@lru_cache(None)
def font(size, bold=False):
    return ImageFont.truetype('/usr/share/fonts/truetype/lato/Lato-' + ('Bold' if bold else 'Regular') + '.ttf', size*S)


def scale(values):
    return tuple(round(v*S) for v in values)


class Frame:
    def __init__(self):
        self.im = Image.new('RGB', (600*S, 600*S), BG)
        self.d = ImageDraw.Draw(self.im)

    def text(self, x, y, text, size=22, fill=INK, bold=False, anchor='mm'):
        self.d.text(scale((x, y)), text, font=font(size, bold), fill=fill, anchor=anchor)

    def line(self, points, fill, width=2):
        self.d.line([scale(p) for p in points], fill=fill, width=round(width*S), joint='curve')

    def circle(self, x, y, r, fill, edge=None, width=1):
        self.d.ellipse(scale((x-r, y-r, x+r, y+r)), fill=fill, outline=edge, width=round(width*S))

    def rounded(self, box, r, fill, edge=None, width=1):
        self.d.rounded_rectangle(scale(box), round(r*S), fill=fill, outline=edge, width=round(width*S))

    def arrow(self, start, end, color, width=2):
        self.line([start, end], color, width)
        a = math.atan2(end[1]-start[1], end[0]-start[0])
        self.line([(end[0]-11*math.cos(a-.45), end[1]-11*math.sin(a-.45)), end,
                   (end[0]-11*math.cos(a+.45), end[1]-11*math.sin(a+.45))], color, width)

    def rating(self, x, y, value, radius=27, color=ORANGE, small=False):
        self.circle(x, y, radius, 'white', color, 1.6)
        self.text(x, y, str(value), 19 if small else 29, color, True)

    def agent(self, x, y):
        self.circle(x, y, 54, PALE_BLUE)
        # Ethereum diamond, drawn as its six contrasting faces.
        faces = (
            ([(0,-44),(-27,0),(0,-12)], '#8c8c8c'),
            ([(0,-44),(0,-12),(27,0)], '#343434'),
            ([(-27,0),(0,16),(0,-12)], '#393939'),
            ([(27,0),(0,-12),(0,16)], '#141414'),
            ([(-27,5),(0,43),(0,21)], '#8c8c8c'),
            ([(27,5),(0,21),(0,43)], '#343434'),
        )
        for points, color in faces:
            self.d.polygon([scale((x+dx, y+dy)) for dx,dy in points], fill=color)


PROJECT = Path(__file__).resolve().parents[2]
TITLE = 'Resisting coordinated rating manipulation in agentic markets'
TITLE_LINES = ('Resisting coordinated rating', 'manipulation in agentic markets')
FPS = 20
DURATION = 9
SIZE = (720, 840)
FIRST_REVIEW = 0.45
SECOND_REVIEW = 1.65
TRAVEL = 0.6
FADE = 0.45
ATTACK_START = 3.3
ATTACK_IMPACT = 4.15
RESET = 8.2
POINTS = [(376,241), (480,239), (521,306), (490,374), (374,371)]

spec = importlib.util.spec_from_file_location('animation_score', PROJECT/'trustlayer/confidence.py')
score_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(score_module)
def review(name, rating, group=None):
    return {'agent_id': 1, 'reviewer': name, 'group': group or name,
            'agent_group': 'agent', 'tag1': 'starred', 'rating': rating}


def score(rows):
    result = score_module.score_agent(rows)
    return {'average': sum(row['rating'] for row in rows) / len(rows) if rows else 0,
            'adjusted': result['score'], 'groups': result['groups']}


rows = [review('a', 40), review('b', 60)]
STAGES = [score([]), score(rows[:1]), score(rows)]
STAGES.append(score(rows + [review(f'linked:{i}', 100, 'b') for i in range(1000)]))
expected = next(case for case in json.loads((PROJECT/'app/static/data/snapshot.json').read_text())['experiments'] if case['id'] == 'linked_wallets')
assert abs(STAGES[-1]['average'] - expected['raw_mean']) < 1e-10
assert STAGES[-1]['adjusted'] == expected['score']
assert STAGES[-1]['groups'] == STAGES[-2]['groups'] == 2
assert STAGES[-1]['adjusted'] == STAGES[-2]['adjusted']
assert ' '.join(TITLE_LINES) == TITLE
assert all(font(29, True).getlength(line) < 540*S for line in TITLE_LINES)


def clamp(p):
    return max(0, min(1, p))


def smooth(p):
    p = clamp(p)
    return p*p*(3-2*p)


def mix(a, b, p):
    p = clamp(p)
    return tuple(round(int(a[i:i+2],16)*(1-p)+int(b[i:i+2],16)*p) for i in (1,3,5))


def score_transition(t):
    # Crossfade exact score labels after each review arrival or attack batch.
    arrivals = (FIRST_REVIEW + TRAVEL, SECOND_REVIEW + TRAVEL, ATTACK_IMPACT)
    for i, arrival in enumerate(arrivals, start=1):
        if t < arrival:
            return STAGES[i-1], STAGES[i-1], 1.0
        if t < arrival + FADE:
            return STAGES[i-1], STAGES[i], smooth((t-arrival) / FADE)
    return STAGES[-1], STAGES[-1], 1.0


def draw_scores(f, t):
    before, after, opacity = score_transition(t)
    box = scale((0, 502, 600, 600))
    old = f.im.crop(box)
    new = old.copy()
    for patch, values in ((old, before), (new, after)):
        d = ImageDraw.Draw(patch)
        for x, key, color in ((48, 'average', ORANGE), (311, 'adjusted', TEAL)):
            d.text(scale((x, 10)), f"{values[key]:.1f}", font=font(72, True), fill=color, anchor='lt')
    f.im.paste(Image.blend(old, new, opacity), box)


def stationary():
    f = Frame()
    f.agent(300,104)
    f.text(50,478,'Average',24,ORANGE,True,'lt')
    f.text(313,478,'Adjusted Score',23,TEAL,True,'lt')
    return f.im


BASE = stationary()


def incoming(f, t, arrival, x, y, value, start, end, color, background=BG):
    visible = smooth((t - arrival) / 0.18)
    if not visible:
        return
    edge = mix(background, color, visible)
    f.arrow(start, end, mix(BG, color, visible * 0.45), 2)
    f.circle(x, y, 32 * (0.8 + 0.2 * visible), mix(background, '#ffffff', visible), edge, 1.6)
    f.text(x, y, str(value), 29, edge, True)
    progress = (t - arrival) / TRAVEL
    if 0 <= progress < 1:
        px = start[0] + (end[0] - start[0]) * progress
        py = start[1] + (end[1] - start[1]) * progress
        f.circle(px, py, 5, color)


def draw(t):
    if t >= RESET:
        return Image.blend(draw(RESET - 0.01), draw(0), smooth((t - RESET) / 0.5))
    f = Frame()
    f.im = BASE.copy()
    f.d = ImageDraw.Draw(f.im)
    group_visible = smooth((t - SECOND_REVIEW) / 0.2)
    if group_visible:
        f.rounded((329,191,561,416), 38, mix(BG, PALE_ORANGE, group_visible),
                  mix(BG, '#edc7b3', group_visible))
        attacking = t >= ATTACK_START
        label = 'Coordinated reviews' if attacking else 'Known reviewer group'
        f.text(445,175,label,18,mix(BG, ORANGE if attacking else MUTED, group_visible), attacking)
    incoming(f, t, FIRST_REVIEW, 140, 310, 40, (154,278), (265,156), '#4a758d')
    incoming(f, t, SECOND_REVIEW, 434, 309, 60, (413,278), (336,161), ORANGE, PALE_ORANGE)
    for i, (x, y) in enumerate(POINTS):
        arrival = ATTACK_START + i * 0.055
        amount = smooth((t - arrival) / 0.16)
        if amount <= 0:
            continue
        edge = mix(PALE_ORANGE, ORANGE, amount)
        f.arrow((x,y), (434,309), mix(PALE_ORANGE, '#e2a588', amount), 1.4)
        progress = (t - arrival) / 0.35
        if 0 <= progress < 1:
            f.circle(x + (434-x)*progress, y + (309-y)*progress, 4, ORANGE)
        radius = 22 * (0.75 + 0.25 * amount)
        f.circle(x,y,radius,mix(PALE_ORANGE, '#ffffff', amount),edge,1.6)
        f.text(x,y,'100',19,edge,True)
    if t >= ATTACK_START:
        # Redraw the original group review above the converging links.
        f.rating(434,309,60,32,ORANGE)
        f.rounded((385,426,503,459),16,'#f6e2d5')
        f.text(444,442,'+1,000',21,ORANGE,True)
        progress = (t - (ATTACK_IMPACT - 0.35)) / 0.35
        if 0 <= progress < 1:
            for offset in (0, 0.13, 0.26):
                p = clamp(progress - offset)
                f.circle(413+(336-413)*p, 278+(161-278)*p, 5-offset*5, ORANGE)
        impact = (t - ATTACK_IMPACT) / 0.5
        if 0 <= impact < 1:
            f.circle(300,104,54+impact*15,None,mix(BG,ORANGE,(1-impact)*0.55),2)
    draw_scores(f, t)
    titled = Image.new('RGB',(600*S,700*S),BG)
    titled.paste(f.im,(0,90*S))
    d = ImageDraw.Draw(titled)
    for line,y in zip(TITLE_LINES,(30,65)):
        d.text(scale((300,y)),line,font=font(29,True),fill=INK,anchor='mm')
    return titled



def render(destination: Path):
    command = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-f', 'rawvideo', '-pixel_format', 'rgb24', '-video_size', f'{SIZE[0]}x{SIZE[1]}',
        '-framerate', str(FPS), '-i', '-', '-an', '-filter_complex',
        'split[a][b];[a]palettegen=max_colors=192:stats_mode=full[p];[b][p]paletteuse=dither=none:diff_mode=rectangle',
        '-loop', '0', '-f', 'gif', str(destination),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    try:
        for i in range(FPS * DURATION):
            frame = draw(i / FPS).resize(SIZE, Image.Resampling.LANCZOS)
            process.stdin.write(frame.tobytes())
            if i % (FPS * 2) == 0:
                print(f'Rendered {i // FPS}/{DURATION} seconds', flush=True)
    finally:
        process.stdin.close()
    if process.wait():
        raise RuntimeError('GIF encoding failed')
    print(destination)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Render the GIF with Pillow, FFmpeg, and Lato fonts.')
    parser.add_argument('--output', type=Path, default=OUT / 'reviewer-manipulation.gif')
    args = parser.parse_args()
    render(args.output)
